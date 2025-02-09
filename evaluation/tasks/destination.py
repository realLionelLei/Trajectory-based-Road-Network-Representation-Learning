# Könnte doch noch klappen wenn man in der Loss funktion die segment ids mit den positionen ersetzt und dann mit torch norm die Distanz als loss berechnet
# Man müsste aber den softmax irgendwie maskieren, weil sonst dauert das einfach ewig

from operator import itemgetter
from typing import Dict, Union

import networkx as nx
import numpy as np
import torch
import torch.nn as nn
from sklearn import model_selection, neighbors
from torch.autograd import Variable
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

from .contextual_plugin import ContextualEmbeddingPlugin
from .task import Task
from .temporal_plugin import NormalPlugin, TemporalEmbeddingPlugin


class DestinationPrediciton(Task):
    """
    Destination Prediction Task. Extract Embeddings for trajectory and put them as sequence into lstm model.
    Predict Destination location given partial trajectory without x % of original trajectory.
    """

    def __init__(
        self,
        traj_dataset,
        network,
        device,
        seed,
        comp_ratio: float = 0.7,
        emb_dim: int = 128,
        batch_size: int = 128,
        epochs: int = 10,
        learning_rate: float = 0.001,
    ):
        self.metrics = {}
        self.data = traj_dataset
        self.network = network
        self.emb_dim = emb_dim
        self.device = device
        self.batch_size = batch_size
        self.lr = learning_rate
        self.epochs = epochs
        self.seed = seed
        self.comp_ratio = comp_ratio

        # make a train test split on trajectorie data
        self.train, self.test = model_selection.train_test_split(
            self.data, test_size=0.2, random_state=self.seed
        )

    def evaluate(self, emb: Union[np.ndarray, nn.Module], plugin: nn.Module = None):
        # assert type(emb) == np.ndarray or isinstance(emb, nn.Module)
        use_temporal = False if plugin == None else True
        train_loader = DataLoader(
            DP_Dataset(self.train, self.network, self.comp_ratio, use_temporal),
            collate_fn=DP_Dataset.collate_fn_padd,
            batch_size=self.batch_size,
            shuffle=True,
        )
        eval_loader = DataLoader(
            DP_Dataset(self.test, self.network, self.comp_ratio, use_temporal),
            collate_fn=DP_Dataset.collate_fn_padd,
            batch_size=self.batch_size,
        )
        model = DP_LSTM(
            out_dim=len(
                self.network.line_graph.nodes
            ),  # predict the destination node (classification)
            device=self.device,
            # pos_map=dict(
            #     zip(
            #         self.network.gdf_edges.fid,
            #         self.network.gdf_edges.geometry.to_crs(
            #             coord_sys
            #         ).centroid,  # portugal specific
            #     )
            # ),
            emb_dim=emb.shape[1],
            # if type(emb) == np.ndarray
            # else emb.embed.tok_embed.weight.shape[1]
            batch_size=self.batch_size,
            plugin=plugin,
            learning_rate=self.lr,
        )

        # train on x trajectories
        model.train_model(loader=train_loader, emb=emb, epochs=self.epochs)

        # eval on test set using distance loss
        yh, y = model.predict(loader=eval_loader, emb=emb)
        res = {}
        for name, (metric, args) in self.metrics.items():
            res[name] = metric(y, yh, **args)

        return res

    def register_metric(self, name, metric_func, args):
        self.metrics[name] = (metric_func, args)

    # @staticmethod
    # def distance_loss(yh: torch.Tensor, y: torch.Tensor, position_map: Dict, **args):
    #     pred_pos = itemgetter(*yh.tolist())(position_map)
    #     label_pos = itemgetter(*y.tolist())(position_map)
    #     # print(pred_pos[0], label_pos[0], pred_pos[0].distance(label_pos[0]))
    #     loss = 0
    #     for p, l in zip(pred_pos, label_pos):
    #         loss += p.distance(l)
    #     # print(loss)
    #     return torch.tensor(loss, requires_grad=True)


class DP_Dataset(Dataset):
    def __init__(self, data, network, comp_ratio: float, _use_time_features=False):
        self.X = (
            data["seg_seq"]
            .swifter.apply(lambda x: x[: int(len(x) * comp_ratio)])
            .values
        )
        self.y = data["seg_seq"].swifter.apply(lambda x: x[-1]).values

        if _use_time_features:
            self.time = data[["dayofweek", "start_hour", "end_hour"]].values

        self.network = network
        self.map = self.create_edge_emb_mapping()
        self._use_time_features = _use_time_features

    def __len__(self):
        return self.X.shape[0]

    def __getitem__(self, idx):
        return (
            torch.tensor(self.X[idx], dtype=int),
            self.y[idx],
            self.time[idx] if self._use_time_features else None,
            self.map,
        )

    # tested index mapping is correct
    def create_edge_emb_mapping(self):
        """_summary_
        Maps fid of edges to index of node (i.e edge) in line_graph
        Returns:
            _type_: _description_
        """
        map = {}
        nodes = list(self.network.line_graph.nodes)
        for index, id in zip(self.network.gdf_edges.index, self.network.gdf_edges.fid):
            map[id] = nodes.index(index)
        # print(map == map2) # yields true

        return map

    @staticmethod
    def collate_fn_padd(batch):
        """
        Padds batch of variable length
        """
        data, label, time, map = zip(*batch)
        # seq length for each input in batch
        lengths_old = torch.tensor([t.shape[0] for t in data])

        # sort data for pad packet, since biggest sequence should be first and then descending order
        sort_idxs = torch.argsort(lengths_old, descending=True, dim=0)
        lengths = lengths_old[sort_idxs]
        data = [
            x
            for _, x in sorted(
                zip(lengths_old.tolist(), data), key=lambda pair: pair[0], reverse=True
            )
        ]
        label = [
            x
            for _, x in sorted(
                zip(lengths_old.tolist(), label), key=lambda pair: pair[0], reverse=True
            )
        ]

        if time != None:
            time = [
                x
                for _, x in sorted(
                    zip(lengths_old.tolist(), time),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
            ]

        # pad
        data = torch.nn.utils.rnn.pad_sequence(data, padding_value=0, batch_first=True)
        # compute mask
        mask = data != 0
        label = [map[0][l] for l in label]  # map to embedding ids

        return (
            data,
            torch.tensor(label, dtype=torch.long),
            torch.tensor(time if time[0] is not None else [], dtype=int),
            lengths,
            mask,
            map[0],
        )


class DP_LSTM(nn.Module):
    def __init__(
        self,
        out_dim: int,
        device,
        emb_dim: int = 128,
        hidden_units: int = 256,
        layers: int = 2,
        batch_size: int = 128,
        plugin=None,
        learning_rate: float = 0.001,
    ):
        super(DP_LSTM, self).__init__()
        print(emb_dim)
        self.encoder = nn.LSTM(
            emb_dim, hidden_units, num_layers=layers, batch_first=True, dropout=0.5
        )
        if plugin is not None and type(plugin) == ContextualEmbeddingPlugin:
            hidden_units = hidden_units * 2
        self.decoder = nn.Sequential(
            nn.Linear(hidden_units, hidden_units * 2),
            nn.ReLU(),
            nn.Linear(hidden_units * 2, hidden_units),
            nn.ReLU(),
            nn.Linear(hidden_units, out_dim),
        )
        self.soft = nn.Softmax(dim=1)
        self.hidden_units = hidden_units
        self.layers = layers
        self.batch_size = batch_size
        self.device = device
        self.loss = nn.CrossEntropyLoss()
        self.opt = torch.optim.Adam(self.parameters(), lr=learning_rate)

        self.plugin = plugin
        self.encoder.to(device)
        self.decoder.to(device)

    def forward(self, x, lengths):
        batch_size, seq_len, _ = x.size()
        self.hidden = self.init_hidden(batch_size=batch_size)

        x = torch.nn.utils.rnn.pack_padded_sequence(x, lengths, batch_first=True)

        x, _ = self.encoder(x)

        x, plengths = torch.nn.utils.rnn.pad_packed_sequence(
            x, batch_first=True, padding_value=0
        )
        x = x.contiguous()  # batch x seq x hidden
        # x = torch.sum(x, dim=1) / lengths.float()
        # x = x.view(-1, x.shape[2])
        x = torch.stack(
            [x[b, plengths[b] - 1] for b in range(batch_size)]
        )  # get last valid item per batch batch x hidden

        if self.plugin is not None and type(self.plugin) == ContextualEmbeddingPlugin:
            x = self.plugin(x)

        yh = self.decoder(x)

        return yh  # (batch x len(possible nodes))

    def train_model(self, loader, emb: Union[np.ndarray, nn.Module], epochs=100):
        self.train()
        for e in tqdm(range(epochs)):
            total_loss = 0
            for X, y, time, lengths, mask, map in tqdm(loader):

                if self.plugin is not None and (
                    type(self.plugin) == TemporalEmbeddingPlugin
                    or type(self.plugin) == NormalPlugin
                ):
                    emb, _ = self.plugin.generate_emb(time)
                    emb = emb.detach().cpu()

                emb_batch = self.get_embedding(emb, X.clone(), mask, map)
                emb_batch = emb_batch.to(self.device)

                if (
                    self.plugin is not None
                    and type(self.plugin) == ContextualEmbeddingPlugin
                ):
                    self.plugin.register_id_seq(X, mask, map, lengths)

                y = y.to(self.device)
                yh = self.forward(emb_batch, lengths)

                loss = self.loss(yh.squeeze(), y)
                self.opt.zero_grad()
                loss.backward()
                self.opt.step()
                total_loss += loss.item()

            print(f"Average training loss in episode {e}: {total_loss/len(loader)}")

    def predict(self, loader, emb: Union[np.ndarray, nn.Module]):
        with torch.no_grad():
            self.eval()
            yhs, ys = [], []
            for X, y, time, lengths, mask, map in loader:

                if self.plugin is not None and (
                    type(self.plugin) == TemporalEmbeddingPlugin
                    or type(self.plugin) == NormalPlugin
                ):
                    emb, _ = self.plugin.generate_emb(time)
                    emb = emb.detach().cpu()

                emb_batch = self.get_embedding(emb, X.clone(), mask, map)
                emb_batch = emb_batch.to(self.device)

                if (
                    self.plugin is not None
                    and type(self.plugin) == ContextualEmbeddingPlugin
                ):
                    self.plugin.register_id_seq(X, mask, map, lengths)

                y = y.to(self.device)
                yh = self.soft(self.forward(emb_batch, lengths)).argmax(dim=1)
                yhs.extend(yh.tolist())
                ys.extend(y.tolist())

            return np.array(yhs), np.array(ys)

    def init_hidden(self, batch_size):
        hidden_a = torch.randn(self.layers, batch_size, self.hidden_units)
        hidden_b = torch.randn(self.layers, batch_size, self.hidden_units)

        hidden_a = Variable(hidden_a)
        hidden_b = Variable(hidden_b)

        hidden_a = hidden_a.to(self.device)
        hidden_b = hidden_b.to(self.device)

        return (hidden_a, hidden_b)

    def get_embedding(self, emb, batch, mask, map):
        """
        Transform batch_size, seq_length, 1 to batch_size, seq_length, emb_size
        """
        if len(emb.shape) < 3:
            emb = emb[None, ...]
        res = torch.zeros((batch.shape[0], batch.shape[1], emb.shape[-1]))
        for i, seq in enumerate(batch):
            idx = i if emb.shape[0] > 1 else 0
            emb_ids = itemgetter(*seq[mask[i]].tolist())(map)
            res[i, mask[i], :] = torch.Tensor(emb[idx, emb_ids, :])

        return res
