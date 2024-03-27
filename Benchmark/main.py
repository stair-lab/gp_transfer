import numpy as np
import torch
import pandas as pd
import matplotlib.pyplot as plt
import pickle
import time
import sys
import argparse
from tqdm import tqdm

import torch
from botorch.models import SingleTaskGP
from botorch.fit import fit_gpytorch_mll
from botorch.utils import standardize
from gpytorch.mlls import ExactMarginalLogLikelihood
from botorch.utils.transforms import normalize, unnormalize, standardize
from gpytorch.kernels import MaternKernel, ScaleKernel, RBFKernel, SpectralMixtureKernel
from models import *

from botorch.optim import optimize_acqf
from botorch.acquisition import qUpperConfidenceBound 
from botorch.acquisition import qExpectedImprovement, qProbabilityOfImprovement, qKnowledgeGradient#, qPredictiveEntropySearch
from botorch.acquisition.max_value_entropy_search import qMaxValueEntropy

#set device here
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu") 
print(device)

def getLookup(trait):
    trait = trait.lower()
    #names = {"narea": "Narea", "sla": "SLA", "ps": "PLSR_SLA_Sorghum", "pn": "FS_PLSR_Narea"}
    #trait = names[trait]

    path = f"/lfs/turing2/0/ruhana/gptransfer/Benchmark/data/{trait}_coh2.csv"
    lookup = pd.read_csv(path, header=0)
    
    ##fix formatting
    lookup_tensor = torch.tensor(lookup.values, dtype=torch.float64)
    no_nan_lookup = torch.nan_to_num(lookup_tensor)
    return no_nan_lookup

def getKernel(kernel_name):
    covar_module = None
    if kernel_name == "matern52": covar_module = ScaleKernel(MaternKernel(nu=5/2, ard_num_dims=2)) 
    elif kernel_name == "matern32": covar_module = ScaleKernel(MaternKernel(nu=3/2, ard_num_dims=2)) 
    elif kernel_name == "matern12": covar_module = ScaleKernel(MaternKernel(nu=1/2, ard_num_dims=2)) 
    elif kernel_name == "rbf": covar_module =  ScaleKernel(RBFKernel())
    elif "spectral" in kernel_name: # (e.g. spectral-10, for spectal with 10 groups)
        _, num_mixtures = kernel_name.split("-")
        num_mixtures = int(num_mixtures)
        covar_module = SpectralMixtureKernel(num_mixtures=num_mixtures, ard_num_dims=2)
    else:
        print("Not a valid kernel") #should also throw error
    return covar_module

def main():
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--env', help='Environment to run search.')
    parser.add_argument('--kernel', default='rbf', help='Kernel function for the gaussian process')
    parser.add_argument('--acq', default='EI', help='Acquisition function')
    parser.add_argument('--n', type=int, default=300, help='Number of iterations')
    
    args = parser.parse_args()
    
    if args.acq == "random": runRandom(args)
    else: runBO(args)
    return 

def runBO(args):
    num_restarts = 20 # 128  
    raw_samples = 128
    q = 1
    n = args.n #replace this
    trait = args.env
    seeds = 3 #consider replacing this
    acq_name = args.acq
    kernel_name = args.kernel
    kernel = getKernel(kernel_name)
    
    #get lookup environment
    lookup = getLookup(args.env)
    bounds = torch.tensor([[0,0],[2150,2150]]).to(device, torch.float64)

    #check the lookup table
    assert torch.isnan(torch.sum(lookup)) == False 
    assert torch.isinf(torch.sum(lookup)) == False

    for seed in range(0,seeds): #seed one is already run and stuff
        tic = time.perf_counter() #start time
        
        ##collect random points as training points
        torch.manual_seed(seed) #setting seed
        train_X = torch.rand(10, 2, dtype=torch.float64, device=device) * 2150
        train_Y = torch.tensor(
            [lookup[int(train_X[i][0]), int(train_X[i][1])] for i in range(0, len(train_X))],
            dtype=torch.float64,
            device=device
        )
        train_Y = train_Y.reshape(-1, 1)

        ##main bayes_opt training loop
        _result = []
        for i in tqdm(range(0,n,q)):
            normalized_x = normalize(train_X, bounds).to(device)
            standardize_y = standardize(train_Y)
            
            if acq_name == "DKL":  
                #initalize & fit model
                model_args = {"learning_rate": 1e-3, 
                              "regnet_dims": [128,128],
                              "regnet_activation": "tanh",
                              "pretrain_steps": 500,
                              "train_steps": 1000,
                             }
                model = SingleTaskDKL(model_args=model_args, input_dim=2, output_dim=1, device=device)
                model.fit_and_save(normalized_x, standardize_y, save_dir=None)
                
                #set acquisition function
                acq = qExpectedImprovement(model, best_f=max(train_Y))
            else:
                gp = SingleTaskGP(
                    normalized_x, standardize_y, 
                    covar_module = kernel,
                    #outcome_transform=Standardize(1), 
                    #input_transform=Normalize(train_X.shape[-1])
                )

                mll = ExactMarginalLogLikelihood(gp.likelihood, gp)
                # load old hyperparameters 
                fit_gpytorch_mll(mll)

                #select acquisition function
                if acq_name == "UCB":
                    acq = qUpperConfidenceBound(gp, beta=0.1)
                elif acq_name == "EI": #working
                    acq = qExpectedImprovement(gp, best_f=max(train_Y))
                elif acq_name == "PI": #working
                    acq = qProbabilityOfImprovement(gp, best_f=max(train_Y))
                elif acq_name == "KG": #working
                    acq = qKnowledgeGradient(gp)  
                else:
                    print(f"{acq_name} is not a valid acquisition function")
            
            #get new point & query new point
            normalized_new_X, acq_value = optimize_acqf(
                acq, 
                q=q, 
                bounds=torch.tensor([[0,0],[1,1]], dtype=torch.float64, device=device), 
                num_restarts=num_restarts, 
                raw_samples=raw_samples)
            new_X = unnormalize(normalized_new_X.detach(), bounds=bounds)
            new_Y = torch.tensor([lookup[int(new_X[i,0]), int(new_X[i,1])] for i in range(len(new_X))], 
                                 dtype=torch.float64, 
                                 device=device).reshape(-1, 1)

            train_X = torch.cat([train_X, new_X])
            train_Y = torch.cat([train_Y, new_Y])

            #end timer and add
            toc = time.perf_counter() #end time
            for i in range(len(new_X)): _result.append([new_Y[i][0].item(), toc - tic, new_X[i]])


        #save all your queries
        torch.save(train_X, f"./output/{trait}/botorch{acq_name}_{kernel_name}_X_{seed}.npy")
        torch.save(train_Y, f"./output/{trait}/botorch{acq_name}_{kernel_name}_Y_{seed}.npy")

        #organize the list to have running best
        best = [0,0,0] # format is [time, best co-heritabilty]
        botorch_result = []
        for i in _result:
            if i[0] > best[0]:
                best = i
            botorch_result.append([best[0], i[1], best[2]]) # append [best so far, current time]
        print("Best From Run: ", best)

        #store results
        botorch_result = pd.DataFrame(botorch_result, columns=["Best", "Time", "Candidate"])
        botorch_result.to_csv(f"./output/{trait}/botorch{acq_name}_{kernel_name}_result_{seed}.npy", encoding='utf8') #store botorch search results

        #print full time
        toc = time.perf_counter() #end time
        print("BoTorch Took ", (toc-tic) ,"seconds")

def runRandom(args):
    n = args.n #replace this
    trait = args.env
    seeds = 5 #consider replacing this
    acq_name = "random"
    
    #get lookup environment
    lookup = getLookup(args.env)
    ub, lb = 2150, 0

    #check the lookup table
    assert torch.isnan(torch.sum(lookup)) == False 
    assert torch.isinf(torch.sum(lookup)) == False
    
    ##main bayes_opt training loop
    train_X = torch.empty((0, 2), dtype=torch.float64, device=device)
    train_Y = torch.empty((0, 1), dtype=torch.float64, device=device)

    for seed in range(0,seeds): #seed one is already run and stuff
        torch.manual_seed(seed)
        tic = time.perf_counter() #start time
        
        _result = []
        for i in tqdm(range(n)):
            new_X = torch.rand((1, 2), dtype=torch.float64, device=device,)
            new_X = new_X * (ub - lb) + lb #adjust to match bounds
            new_Y = torch.tensor([lookup[int(new_X[0][0]), int(new_X[0][1])]], 
                                 dtype=torch.float64, 
                                 device=device).reshape(-1, 1)
            #add new candidate
            train_X = torch.cat([train_X, new_X])
            train_Y = torch.cat([train_Y, new_Y])

            #end timer and add
            toc = time.perf_counter() #end time
            _result.append([new_Y[0][0].item(), toc - tic, new_X[0]])

        #save all your queries
        torch.save(train_X, f"./output/{trait}/botorch{acq_name}_X_{seed}.npy")
        torch.save(train_Y, f"./output/{trait}/botorch{acq_name}_Y_{seed}.npy")

        #organize the list to have running best
        best = [0,0,0] # format is [time, best co-heritabilty]
        botorch_result = []
        for i in _result:
            if i[0] > best[0]:
                best = i
            botorch_result.append([best[0], i[1], best[2]]) # append [best so far, current time]
        print("Best From Run: ", best)

        #store results
        botorch_result = pd.DataFrame(botorch_result, columns=["Best", "Time", "Candidate"])
        botorch_result.to_csv(f"./output/{trait}/botorch{acq_name}_result_{seed}.npy", encoding='utf8') #store botorch search results

        #print full time
        toc = time.perf_counter() #end time
        print("BoTorch Took ", (toc-tic) ,"seconds")
    
    return        
        
main()

#add other acquisition function options
#make sure that data loading works correctly