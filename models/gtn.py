import os
from ast import walk
from operator import itemgetter
from turtle import forward

import networkx as nx
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from _walker import random_walks as _random_walks
from scipy import sparse
from torch.utils.data import DataLoader, Dataset
from torch_geometric.nn import GAE, GATConv, GCNConv, InnerProductDecoder
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.nn.norm import LayerNorm
from torch_geometric.utils import (add_self_loops, from_networkx,
                                   negative_sampling, remove_self_loops)
from torch_geometric.utils.num_nodes import maybe_num_nodes
from torch_sparse import SparseTensor
from tqdm import tqdm

from .model import Model


class GTNModel(Model):
    def __init__(
        self,
        data,
        adj,
        device,
        network,
        traj_data,
    ):
        self.conv_model = GTCModel(data, device, network, traj_data, adj=adj)
        self.struct_model = Traj2VecModel(data, network, adj, device)

    def train(self):
        ...


class TrajectoryTransformer(nn.Module):
    def __init__(self):
        ...


class GTCModel(Model):
    def __init__(
        self,
        data,
        device,
        network,
        traj_data,
        emb_dim: str = 128,
        adj=None,
        k: int = 1,
        bidirectional=True,
        add_self_loops=True,
        norm=False,
    ):
        self.model = GTNSubConv(data.x.shape[1], emb_dim, norm=norm)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=0.01)
        self.model = self.model.to(device)
        self.device = device
        self.traj_data = traj_data["seg_seq"].tolist()
        self.network = network
        self.traj_to_node = self.generate_trajid_to_nodeid()
        if adj is None:
            adj = self.generate_node_traj_adj(
                k=k, bidirectional=bidirectional, add_self_loops=add_self_loops
            )
            np.savetxt(
                "./traj_adj_k_" + str(k) + "_" + str(bidirectional) + ".gz", X=adj
            )
        # else:
        # adj = np.loadtxt(load_traj_adj_path)

        self.train_data = self.transform_data(data, adj)
        self.train_data = self.train_data.to(device)

    def train(self, epochs: int = 1000):
        avg_loss = 0
        for e in range(epochs):
            self.model.train()
            self.optimizer.zero_grad()
            z = self.model(
                self.train_data.x,
                self.train_data.edge_traj_index,
                self.train_data.edge_weight,
            )
            loss = self.recon_loss(z, self.train_data.edge_traj_index)
            loss.backward()
            self.optimizer.step()
            avg_loss += loss.item()

            # if e > 0 and e % 1000 == 0:
            #     print("Epoch: {}, avg_loss: {}".format(e, avg_loss / e))

    def recon_loss(self, z, pos_edge_index, neg_edge_index=None):
        decoder = InnerProductDecoder()
        EPS = 1e-15

        pos_loss = -torch.log(decoder(z, pos_edge_index, sigmoid=True) + EPS).mean()

        # Do not include self-loops in negative samples
        pos_edge_index, _ = remove_self_loops(pos_edge_index)
        pos_edge_index, _ = add_self_loops(pos_edge_index)
        if neg_edge_index is None:
            neg_edge_index = negative_sampling(pos_edge_index, z.size(0))
        neg_loss = -torch.log(1 - decoder(z, neg_edge_index, sigmoid=True) + EPS).mean()

        return pos_loss + neg_loss

    def transform_data(self, data, adj):
        G = nx.from_numpy_array(adj.T, create_using=nx.DiGraph)
        data_traj = from_networkx(G)
        data.edge_traj_index = data_traj.edge_index
        data.edge_weight = data_traj.weight

        return data

    def generate_node_traj_adj(
        self, k: int = np.inf, bidirectional=True, add_self_loops=True
    ):
        nodes = list(self.network.line_graph.nodes)
        adj = nx.to_numpy_array(self.network.line_graph)
        np.fill_diagonal(adj, 0)

        if add_self_loops:
            adj += np.eye(len(nodes), len(nodes))

        for traj in tqdm(self.traj_data):
            # print(traj)
            for i, traj_node in enumerate(traj):
                if k == -1:
                    k = len(traj)
                left_slice, right_slice = min(k, i) if bidirectional else 0, min(
                    k + 1, len(traj) - i
                )
                traj_nodes = traj[(i - left_slice) : (i + right_slice)]
                # convert traj_nodes to graph_nodes
                target = itemgetter(traj_node)(self.traj_to_node)
                context = itemgetter(*traj_nodes)(self.traj_to_node)
                adj[target, context] += 1
        # remove self weighting if no self loops should be allowed
        if not add_self_loops:
            np.fill_diagonal(adj, 0)
            zero_rows = np.where(~adj.any(axis=1))[0]
            for idx in zero_rows:
                adj[idx, idx] = 1

        # norm adj row wise to get probs
        rowsum = adj.sum(axis=1, keepdims=True)
        adj = adj / rowsum

        # convert to edge_index

        return adj

    def generate_trajid_to_nodeid(self):
        map = {}
        nodes = list(self.network.line_graph.nodes)
        for index, id in zip(self.network.gdf_edges.index, self.network.gdf_edges.fid):
            map[id] = nodes.index(index)

        return map

    def save_model(self, path="save/"):
        torch.save(self.model.state_dict(), os.path.join(path + "/model.pt"))

    def load_model(self, path):
        self.model.load_state_dict(torch.load(path, map_location=self.device))

    def save_emb(self, path):
        np.savetxt(
            os.path.join(path + "/embedding.out"),
            X=self.model.encode(self.train_data.x, self.train_data.edge_index)
            .detach()
            .cpu()
            .numpy(),
        )

    def load_emb(self, path=None):
        if path:
            return np.loadtxt(path)
        return (
            self.model(
                self.train_data.x,
                self.train_data.edge_traj_index,
                self.train_data.edge_weight,
            )
            .detach()
            .cpu()
        )


class GTNSubConv(nn.Module):
    def __init__(self, in_dim: int, out_dim: int, norm=False):
        super().__init__()
        self.conv = GCNConv(in_dim, out_dim, cached=True)
        self.norm_layer = LayerNorm(out_dim)
        self.norm = norm

    def forward(self, x, edge_index, edge_weight):
        x = self.conv(x.float(), edge_index, edge_weight)
        if self.norm:
            x = self.norm_layer(x)
        return x


class GTNConv(MessagePassing):
    def __init__(self, in_channels: int, out_channels: int):
        ...


class Traj2VecModel(Model):
    def __init__(
        self,
        data,
        network,
        adj,
        device,
        emb_dim=128,
        walk_length=30,
        walks_per_node=25,
        context_size=5,
        num_neg=10,
    ):
        self.model = Traj2Vec(
            data.edge_index,
            network,
            adj,
            embedding_dim=emb_dim,
            walk_length=walk_length,
            context_size=context_size,
            walks_per_node=walks_per_node,
            num_negative_samples=num_neg,
            sparse=True,
        ).to(device)
        self.loader = self.model.loader(batch_size=128, shuffle=True, num_workers=4)
        self.device = device
        self.data = data
        self.optimizer = torch.optim.SparseAdam(list(self.model.parameters()), lr=0.01)

    def train(self, epochs):
        avg_loss = 0
        for e in range(epochs):
            self.model.train()
            total_loss = 0
            for pos_rw, neg_rw in self.loader:
                self.optimizer.zero_grad()
                loss = self.model.loss(pos_rw.to(self.device), neg_rw.to(self.device))
                loss.backward()
                self.optimizer.step()
                total_loss += loss.item()
            avg_loss += total_loss / len(self.loader)
            if e > 0 and e % 1 == 0:
                print("Epoch: {}, avg_loss: {}".format(e, avg_loss / e))

    def save_model(self, path="save/"):
        torch.save(self.model.state_dict(), path + "model.pt")

    def load_model(self, path):
        self.model.load_state_dict(torch.load(path, map_location=self.device))

    def save_emb(self, path):
        np.savetxt(path + "embedding.out", X=self.model().detach().cpu().numpy())

    def load_emb(self, path=None):
        if path:
            self.emb = np.loadtxt(path)
        return self.model().detach().cpu().numpy()


class Traj2Vec(nn.Module):
    def __init__(
        self,
        edge_index,
        network,
        adj,
        embedding_dim,
        walk_length,
        context_size,
        walks_per_node=1,
        num_negative_samples=1,
        num_nodes=None,
        sparse=True,
    ):
        super().__init__()

        N = maybe_num_nodes(edge_index, num_nodes)
        self.adj = adj  # SparseTensor(row=row, col=col, sparse_sizes=(N, N))
        # self.adj = self.adj.to("cpu")
        self.network = network
        # self.traj_data = Traj2Vec.map_traj_to_node_ids(traj_data, network)
        self.EPS = 1e-15

        assert walk_length >= context_size

        self.embedding_dim = embedding_dim
        self.walk_length = walk_length - 1
        self.context_size = context_size
        self.walks_per_node = walks_per_node
        self.num_negative_samples = num_negative_samples

        self.embedding = nn.Embedding(N, embedding_dim, sparse=sparse)

        self.reset_parameters()

    def reset_parameters(self):
        self.embedding.reset_parameters()

    def forward(self, batch=None):
        """Returns the embeddings for the nodes in :obj:`batch`."""
        emb = self.embedding.weight
        return emb if batch is None else emb.index_select(0, batch)

    def loader(self, **kwargs):
        return DataLoader(range(self.adj.shape[0]), collate_fn=self.sample, **kwargs)

    @staticmethod
    def traj_walk(adj, walk_length, start, walks_per_node):
        # create index matrix
        # matrix = np.tile(np.arange(0, adj.shape[0]), reps=(adj.shape[0], 1))
        # samples = []
        # for _ in range(walks_per_node):
        #     samples.append(
        #         np.array(
        #             [
        #                 np.random.choice(matrix[i], p=adj[i], size=walk_length)
        #                 for i in range(adj.shape[0])
        #             ]
        #         )
        #     )
        # walks = np.zeros(shape=(len(start) * walks_per_node, walk_length + 1))
        # run_idx = 0
        # for n in range(walks_per_node):
        #     for i, s in enumerate(start):
        #         walk = [s]
        #         curr_idx = s
        #         for j in range(walk_length):
        #             walk.append(samples[n][curr_idx, j])
        #             curr_idx = walk[-1]
        #         walks[run_idx + i, :] = walk
        #     run_idx += len(start)

        A = sparse.csr_matrix(adj)
        indptr = A.indptr.astype(np.uint32)
        indices = A.indices.astype(np.uint32)
        data = A.data.astype(np.float32)

        walks = _random_walks(
            indptr, indices, data, start, walks_per_node, walk_length + 1
        )

        return walks.astype(int)

    @staticmethod
    def generate_traj_static_walks(traj_data, network, walk_length):
        # create map
        tmap = {}
        nodes = list(network.line_graph.nodes)
        for index, id in zip(network.gdf_edges.index, network.gdf_edges.fid):
            tmap[id] = nodes.index(index)

        # map traj id sequences to graph node id sequences
        mapped_traj = []
        for traj in traj_data:
            mapped_traj.append(itemgetter(*traj)(tmap))

        # generate matrix with walk length columns
        traj_matrix = np.zeros(
            shape=(
                sum(len(x) for x in mapped_traj)
                - (len(mapped_traj) * (walk_length))
                + 1,
                walk_length,
            )
        )
        run_idx = 0
        for j, traj in tqdm(enumerate(mapped_traj)):
            for i in range(len(traj)):
                if walk_length > len(traj) - i:
                    break
                window = i + walk_length
                traj_matrix[run_idx + i, :] = traj[i:window]
            run_idx += len(traj) - walk_length

        return traj_matrix

    def pos_sample(self, batch):
        # batch = batch.repeat(self.walks_per_node)
        # rowptr, col, _ = self.adj.csr()
        rw = torch.tensor(
            Traj2Vec.traj_walk(
                self.adj,
                self.walk_length,
                start=batch,
                walks_per_node=self.walks_per_node,
            ),
            dtype=int,
        )
        if not isinstance(rw, torch.Tensor):
            rw = rw[0]

        walks = []
        num_walks_per_rw = 1 + self.walk_length + 1 - self.context_size
        for j in range(num_walks_per_rw):
            walks.append(rw[:, j : j + self.context_size])
        return torch.cat(walks, dim=0)

    def neg_sample(self, batch):
        batch = batch.repeat(self.walks_per_node * self.num_negative_samples)

        rw = torch.randint(self.adj.shape[0], (batch.size(0), self.walk_length))
        rw = torch.cat([batch.view(-1, 1), rw], dim=-1)

        walks = []
        num_walks_per_rw = 1 + self.walk_length + 1 - self.context_size
        for j in range(num_walks_per_rw):
            walks.append(rw[:, j : j + self.context_size])
        return torch.cat(walks, dim=0)

    def sample(self, batch):
        if not isinstance(batch, torch.Tensor):
            batch = torch.tensor(batch)
        return self.pos_sample(batch), self.neg_sample(batch)

    def loss(self, pos_rw, neg_rw):
        r"""Computes the loss given positive and negative random walks."""

        # Positive loss.
        start, rest = pos_rw[:, 0], pos_rw[:, 1:].contiguous()

        h_start = self.embedding(start).view(pos_rw.size(0), 1, self.embedding_dim)
        h_rest = self.embedding(rest.view(-1)).view(
            pos_rw.size(0), -1, self.embedding_dim
        )

        out = (h_start * h_rest).sum(dim=-1).view(-1)
        pos_loss = -torch.log(torch.sigmoid(out) + self.EPS).mean()

        # Negative loss.
        start, rest = neg_rw[:, 0], neg_rw[:, 1:].contiguous()

        h_start = self.embedding(start).view(neg_rw.size(0), 1, self.embedding_dim)
        h_rest = self.embedding(rest.view(-1)).view(
            neg_rw.size(0), -1, self.embedding_dim
        )

        out = (h_start * h_rest).sum(dim=-1).view(-1)
        neg_loss = -torch.log(1 - torch.sigmoid(out) + self.EPS).mean()

        return pos_loss + neg_loss
