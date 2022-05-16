from dataclasses import dataclass, field

import fmm
import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import swifter
from fmm import STMATCH, Network, NetworkGraph, STMATCHConfig
from shapely.geometry import LineString


class RoadNetwork:
    """
    Class representing a Road Network.
    """

    G: nx.MultiDiGraph
    gdf_nodes: gpd.GeoDataFrame
    gdf_edges: gpd.GeoDataFrame

    def __init__(
        self,
        location: str,
        network_type: str = "roads",
        retain_all: bool = True,
        truncate_by_edge: bool = True,
    ):
        """
        Create network from edge and node file or from a osm query string.

        Args:
            location (str): _description_
            network_type (str, optional): _description_. Defaults to "roads".
            retain_all (bool, optional): _description_. Defaults to True.
            truncate_by_edge (bool, optional): _description_. Defaults to True.
        """
        self.G = ox.graph_from_place(
            location,
            network_type=network_type,
            retain_all=retain_all,
            truncate_by_edge=truncate_by_edge,
        )
        self.gdf_nodes, self.gdf_edges = ox.graph_to_gdfs(self.G)

    def map_trajectorie(self, coordinates: gpd.GeoDataFrame):
        P = ox.project_graph(self.G)
        B = ox.project_gdf(coordinates)

        X = B.geometry.apply(lambda c: c.centroid.x)
        Y = B.geometry.apply(lambda c: c.centroid.y)

        mapping = ox.nearest_edges(P, X, Y, return_dist=True)

        print(mapping[:10])

    def save(self, path: str):
        """
        Save road network as node and edge shape file.
        Args:
            path (str): file saving path
        """
        gdf_nodes, gdf_edges = ox.graph_to_gdfs(self.G)
        gdf_nodes = ox.io._stringify_nonnumeric_cols(gdf_nodes)
        gdf_edges = ox.io._stringify_nonnumeric_cols(gdf_edges)
        gdf_edges["fid"] = np.arange(0, gdf_edges.shape[0], dtype="int")

        gdf_nodes.to_file(path + "/nodes.shp", encoding="utf-8")
        gdf_edges.to_file(path + "/edges.shp", encoding="utf-8")

    def fmm_trajectorie_mapping(self):

        network = Network("../osm_data/porto/edges.shp", "fid", "u", "v")
        graph = NetworkGraph(network)

        stmatch_model = STMATCH(network, graph)

        k = 16
        gps_error = 0.0005
        radius = 0.003
        vmax = 0.0003
        factor = 1.5
        stmatch_config = STMATCHConfig(k, radius, gps_error, vmax, factor)

        input_config = fmm.GPSConfig()
        input_config.file = "../datasets/trajectories/Porto/mapped_id_poly.csv"
        input_config.id = "id"
        input_config.geom = "POLYLINE"
        print(input_config.to_string())

        result_config = fmm.ResultConfig()
        result_config.file = "../datasets/trajectories/Porto/road-segment-mapping.txt"
        result_config.output_config.write_opath = True
        result_config.output_config.write_spdist = True
        result_config.output_config.write_speed = True
        result_config.output_config.write_duration = True
        print(result_config.to_string())

        status = stmatch_model.match_gps_file(
            input_config, result_config, stmatch_config, use_omp=True
        )
        print(status)

    @property
    def bounds(self):
        return self.gdf_nodes.geometry.total_bounds

    def visualize():
        ...
        # define the colors to use for different edge types
        # hwy_colors = {'footway': 'skyblue',
        #             'residential': 'paleturquoise',
        #             'cycleway': 'orange',
        #             'service': 'sienna',
        #             'living street': 'lightgreen',
        #             'secondary': 'grey',
        #             'pedestrian': 'lightskyblue'}

        # # return edge IDs that do not match passed list of hwys
        # def find_edges(G, hwys):
        #     edges = []
        #     for u, v, k, data in G.edges(keys=True, data='highway'):
        #         check1 = isinstance(data, str) and data not in hwys
        #         check2 = isinstance(data, list) and all([d not in hwys for d in data])
        #         if check1 or check2:
        #             edges.append((u, v, k))
        #     return set(edges)

        # # first plot all edges that do not appear in hwy_colors's types
        # G_tmp = G.copy()
        # G_tmp.remove_edges_from(G.edges - find_edges(G, hwy_colors.keys()))
        # m = ox.plot_graph_folium(G_tmp, popup_attribute='highway', weight=5, color='black')

        # # then plot each edge type in hwy_colors one at a time
        # for hwy, color in hwy_colors.items():
        #     G_tmp = G.copy()
        #     G_tmp.remove_edges_from(find_edges(G_tmp, [hwy]))
        #     if G_tmp.edges:
        #         m = ox.plot_graph_folium(G_tmp,
        #                                 graph_map=m,
        #                                 popup_attribute='highway',
        #                                 weight=5,
        #                                 color=color)
        # m