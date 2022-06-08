from ast import literal_eval
from collections import Counter
from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd
import swifter
from tqdm import tqdm


class Trajectory:
    """
    Trajectory class which has methods to get data from a trajectory dataframe.
    """

    def __init__(self, df_path: str, nrows=-1):
        self.df = pd.read_csv(df_path, sep=";", nrows=nrows)
        # preprocess column types
        self.df["cpath"] = self.df["cpath"].swifter.apply(
            lambda x: np.fromstring(
                x.replace("\n", "").replace("(", "").replace(")", "").replace(" ", ""),
                sep=",",
                dtype=np.int,
            )
        )
        self.df["opath"] = self.df["opath"].swifter.apply(
            lambda x: np.fromstring(
                x.replace("\n", "").replace("(", "").replace(")", "").replace(" ", ""),
                sep=",",
                dtype=np.int,
            )
        )
        self.df["speed"] = self.df["speed"].swifter.apply(
            lambda x: np.fromstring(
                x.replace("\n", "")
                .replace("[", "")
                .replace("]", "")
                .replace("  ", " "),
                sep=" ",
            )
        )

    def generate_TTE_datatset(self):
        """
        Generates dataset for TimeTravel Estimation.
        Returns dataframe with traversed road segments and needed time.
        """
        tte = self.df[["id", "cpath", "duration"]].copy()
        tte["duration"] = tte["duration"].swifter.apply(literal_eval)
        tte["travel_time"] = tte["duration"].swifter.apply(np.sum)
        tte.drop("duration", axis=1, inplace=True)

        return tte.rename(columns={"cpath": "seg_seq"})

    def generate_speed_features(self, network) -> pd.DataFrame:
        """
        Generates features containing average speed, utilization and accelaration
        for each edge i.e road segment.

        Returns:
            pd.DataFrame: features in shape num_edges x features
        """
        rdf = pd.DataFrame({"id": np.arange(network.gdf_edges.shape[0])})
        # calculate utilization on each edge which is defined as the count an edge is traversed by all trajectories
        seg_seqs = self.df["cpath"].values
        counter = Counter()
        for seq in seg_seqs:
            counter.update(Counter(seq))

        rdf["util"] = rdf.index.map(counter)
        # rdf["util"] = (rdf["util"] - rdf["util"].min()) / (
        #    rdf["util"].max() - rdf["util"].min()
        # )  # min max normalization

        # print(rdf["util"].describe())
        # print(rdf[rdf["util"] == 0])

        # generate average speed feature
        # little bit complicater

        # print(self.df["opath"][0], self.df["cpath"][0])
        # key: edge_id, value: tuple[speed, count]
        cpaths = self.df["cpath"].values
        opaths = self.df["opath"].values
        speeds = self.df["speed"].values
        speed_counter = Counter()
        count_counter = Counter()

        for opath, cpath, speed in tqdm(zip(opaths, cpaths, speeds)):
            last_lidx, last_ridx = 0, 0
            for l, r, s in zip(opath[0::1], opath[1::1], speed):
                # print(l, r, s)
                lidxs, ridxs = np.where(cpath == l)[0], np.where(cpath == r)[0]
                lidx, ridx = (
                    lidxs[lidxs >= last_lidx][0],
                    ridxs[ridxs >= last_ridx][0],
                )
                assert lidx <= ridx
                traversed_edges = cpath[lidx : ridx + 1]
                # print(traversed_edges)
                speed_counter.update(
                    dict(zip(traversed_edges, [s] * len(traversed_edges)))
                )
                count_counter.update(
                    dict(zip(traversed_edges, [1] * len(traversed_edges)))
                )
                last_lidx, last_ridx = lidx, ridx

        rdf["avg_speed"] = rdf.index.map(
            {
                k: (float(speed_counter[k]) / count_counter[k]) * 111000 * 3.6
                for k in speed_counter
            }
        )  # calculate average speed in km/h

        # rdf["avg_speed"] = (rdf["avg_speed"] - rdf["avg_speed"].min()) / (
        #    rdf["avg_speed"].max() - rdf["avg_speed"].min()
        # )

        return rdf