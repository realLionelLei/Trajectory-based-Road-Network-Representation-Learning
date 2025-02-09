"""
Generate scores for GTC Layer with different k-values for forward and bidirectional setting
Split with different seeds to get better approximation
"""

import argparse
import os
import random
import sys
from unittest import result

module_path = os.path.abspath(os.path.join(".."))
if module_path not in sys.path:
    sys.path.append(module_path)

import numpy as np
import pandas as pd
import torch
from models import GTCModel

from evaluate_models import generate_dataset
from evaluation import Evaluation
from tasks.task_loader import *


def evaluate_k(args, data, network, trajectory):
    seeds = [42, 69, 88, 420, 1234]
    ks = [1, 2, 3, 4, 5, 6]

    torch.backends.cudnn.deterministic = True
    torch.cuda.set_device(args["device"])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    results = []
    # preload adja matrices
    adjas_bidir = [
        np.loadtxt(
            "../models/training/gtn_precalc_adj/traj_adj_k_{}_porto.gz".format(k)
        )
        for k in ks
    ]
    adjas_forward = [
        np.loadtxt(
            "../models/training/gtn_precalc_adj/traj_adj_k_{}_False_porto.gz".format(k)
        )
        for k in ks
    ]

    for seed in seeds:
        print(f"START Evaluation with seed {seed}")
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        args["seed"] = seed
        args["epochs"] = 10
        tasks = [t for t in args["tasks"].split(",")]

        traj_train = trajectory.sample(args["sample_size"], axis=0, random_state=seed)

        eva = Evaluation()
        if "roadclf" in tasks:
            eva.register_task("roadclf", init_roadclf(args, network, seed=seed))
        if "meanspeed" in tasks:
            eva.register_task("meanspeed", init_meanspeed(args, network, seed=seed))
        if "traveltime" in tasks:
            eva.register_task(
                "traveltime",
                init_traveltime(args, traj_train, network, device, seed=seed),
            )
        if "nextlocation" in tasks:
            eva.register_task(
                "nextlocation",
                init_nextlocation(args, traj_train, network, device, seed=seed),
            )
        if "destination" in tasks:
            eva.register_task(
                "destination",
                init_destination(args, traj_train, network, device, seed=seed),
            )

        for k in ks:
            model_bi = GTCModel(
                data,
                device,
                network,
                adj=adjas_bidir[k - 1],
            )
            model_for = GTCModel(
                data,
                device,
                network,
                adj=adjas_forward[k - 1],
            )
            model_bi.train(epochs=5000)
            model_for.train(epochs=5000)

            eva.register_model("gtc_k_{}_bidirectional".format(k), model_bi, {})
            eva.register_model("gtc_k_{}_forward".format(k), model_for, {})

        seed_results = eva.run()

        # concatinate results
        if len(results) == 0:
            for name, res in seed_results:
                res["seed"] = seed
                results.append((name, res))
        else:
            for i, ((name, full_res), (_, seed_res)) in enumerate(
                zip(results, seed_results)
            ):
                seed_res["seed"] = seed
                results[i] = (name, pd.concat([full_res, seed_res], axis=0))

    path = os.path.join(args["path"], "gtc_k_eva")
    if not os.path.exists(path):
        os.mkdir(path)
    for name, res in results:
        res.to_csv(path + "/" + name + ".csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embedding Evaluation")
    parser.add_argument(
        "-t", "--tasks", help="Tasks to evaluate on", required=True, type=str
    )
    parser.add_argument(
        "-s",
        "--speed",
        help="Include speed features to include",
        type=str,
    )
    parser.add_argument(
        "-p",
        "--path",
        help="Save path evaluation results",
        required=True,
        type=str,
    )
    parser.add_argument(
        "-d", "--device", help="Cuda device number", type=int, required=True
    )

    parser.add_argument(
        "-r",
        "--nrows",
        help="Trajectory sample count to load for evaluation",
        type=int,
        required=True,
    )

    parser.add_argument(
        "-ss",
        "--sample_size",
        help="Trajectory sample count to sample for evaluation",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-dl",
        "--drop_label",
        help="remove label from train dataset",
        type=str,
    )

    args = vars(parser.parse_args())
    args["city"] = "porto"
    args["batch_size"] = 512

    network, trajectory, data = generate_dataset(args, 69)  # test split for the dataset
    evaluate_k(args, data, network, trajectory)
