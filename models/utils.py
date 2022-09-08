import numpy as np
import torch
import networkx as nx
from torch_geometric.utils import from_networkx


def generate_trajid_to_nodeid(network):
    map = {}
    nodes = list(network.line_graph.nodes)
    for index, id in zip(network.gdf_edges.index, network.gdf_edges.fid):
        map[id] = nodes.index(index)

    return map


def transform_data(data, adj):
    G = nx.from_numpy_array(adj.T, create_using=nx.DiGraph)
    data_traj = from_networkx(G)
    data.edge_traj_index = data_traj.edge_index
    data.edge_weight = data_traj.weight

    return data


def generate_dataset(
    data,
    seq_len,
    pre_len,
    time_len=None,
    reconstruct=False,
    split_ratio=0.8,
    normalize=True,
):
    """
    https://github.com/lehaifeng/T-GCN/blob/master/T-GCN/T-GCN-PyTorch/utils/data/functions.py
    :param data: feature matrix
    :param seq_len: length of the train data sequence
    :param pre_len: length of the prediction data sequence
    :param time_len: length of the time series in total
    :param split_ratio: proportion of the training set
    :param normalize: scale the data to (0, 1], divide by the maximum value in the data
    :return: train set (X, Y) and test set (X, Y)
    """
    if time_len is None:
        time_len = data.shape[0]
    if normalize:
        max_val = np.max(data)
        data = data / max_val
    train_size = int(time_len * split_ratio)
    train_data = data[:train_size]
    test_data = data[train_size:time_len]
    train_X, train_Y, test_X, test_Y = list(), list(), list(), list()
    t = seq_len if reconstruct else seq_len - pre_len
    for i in range(len(train_data) - t):
        train_X.append(np.array(train_data[i : i + seq_len]))
        if reconstruct:
            train_Y.append(np.array(train_data[i : i + seq_len]))
        else:
            train_Y.append(np.array(train_data[i + seq_len : i + seq_len + pre_len]))
    for i in range(len(test_data) - seq_len - pre_len):
        test_X.append(np.array(test_data[i : i + seq_len]))
        test_Y.append(np.array(test_data[i + seq_len : i + seq_len + pre_len]))
    return np.array(train_X), np.array(train_Y), np.array(test_X), np.array(test_Y)


def generate_torch_datasets(
    data,
    seq_len,
    pre_len,
    time_len=None,
    reconstruct=False,
    split_ratio=0.8,
    normalize=True,
):
    train_X, train_Y, test_X, test_Y = generate_dataset(
        data,
        seq_len,
        pre_len,
        time_len=time_len,
        reconstruct=reconstruct,
        split_ratio=split_ratio,
        normalize=normalize,
    )
    train_dataset = torch.utils.data.TensorDataset(
        torch.FloatTensor(train_X), torch.FloatTensor(train_Y)
    )
    test_dataset = torch.utils.data.TensorDataset(
        torch.FloatTensor(test_X), torch.FloatTensor(test_Y)
    )
    return train_dataset, test_dataset
