import torch
from torch import Tensor
import torch.nn as nn
import dgl

try:
    from dgl import DGLGraph
except:
    raise ImportError(
        "Mesh Graph Net requires the DGL library. Install the "
        + "desired CUDA version at: \n https://www.dgl.ai/pages/start.html"
    )
from typing import Callable, Tuple, List, Union
from dataclasses import dataclass

from modulus.models.meshgraphnet.meshgraphnet import MeshGraphNet
import torch_cluster
import torch_scatter


class Mesh_Reduced(torch.nn.Module):
    """PbGMR-GMUS architecture
    Parameters
    ----------
    input_dim_nodes : int
        Number of node features
    input_dim_edges : int
        Number of edge features
    output_decode_dim: int
        Number of decoding outputs (per node)
    output_encode_dim: int, optional
        Number of encoding outputs (per pivotal postion),  by default 3
    processor_size : int, optional
        Number of message passing blocks, by default 15
    num_layers_node_processor : int, optional
        Number of MLP layers for processing nodes in each message passing block, by default 2
    num_layers_edge_processor : int, optional
        Number of MLP layers for processing edge features in each message passing block, by default 2
    hidden_dim_processor : int, optional
        Hidden layer size for the message passing blocks, by default 128
    hidden_dim_node_encoder : int, optional
        Hidden layer size for the node feature encoder, by default 128
    num_layers_node_encoder : int, optional
        Number of MLP layers for the node feature encoder, by default 2
    hidden_dim_edge_encoder : int, optional
        Hidden layer size for the edge feature encoder, by default 128
    num_layers_edge_encoder : int, optional
        Number of MLP layers for the edge feature encoder, by default 2
    hidden_dim_node_decoder : int, optional
        Hidden layer size for the node feature decoder, by default 128
    num_layers_node_decoder : int, optional
        Number of MLP layers for the node feature decoder, by default 2
    k: int, optional
        Number of nodes considered for per pivotal postion, by default 3
    aggregation: str, optional
        Message aggregation type, by default "mean"
    Note
    ----
    Reference: Han, Xu, et al. "Predicting physics in mesh-reduced space with temporal attention."
    arXiv preprint arXiv:2201.09113 (2022).

    """

    def __init__(
        self,
        input_dim_nodes: int,
        input_dim_edges: int,
        output_decode_dim: int,
        output_encode_dim: int = 3,
        processor_size: int = 15,
        num_layers_node_processor: int = 2,
        num_layers_edge_processor: int = 2,
        hidden_dim_processor: int = 128,
        hidden_dim_node_encoder: int = 128,
        num_layers_node_encoder: int = 2,
        hidden_dim_edge_encoder: int = 128,
        num_layers_edge_encoder: int = 2,
        hidden_dim_node_decoder: int = 128,
        num_layers_node_decoder: int = 2,
        k: int = 3,
        knn_every_step: bool = False,
        aggregation: str = "mean",
    ):
        super(Mesh_Reduced, self).__init__()
        self.knn_every_step = knn_every_step
        self.knn_encoder_already = False
        self.knn_decoder_already = False
        self.encoder_processor = MeshGraphNet(
            input_dim_nodes,
            input_dim_edges,
            output_encode_dim,
            processor_size,
            "relu",
            num_layers_node_processor,
            num_layers_edge_processor,
            hidden_dim_processor,
            hidden_dim_node_encoder,
            num_layers_node_encoder,
            hidden_dim_edge_encoder,
            num_layers_edge_encoder,
            hidden_dim_node_decoder,
            num_layers_node_decoder,
            aggregation,
        )
        self.decoder_processor = MeshGraphNet(
            output_encode_dim,
            input_dim_edges,
            output_decode_dim,
            processor_size,
            "relu",
            num_layers_node_processor,
            num_layers_edge_processor,
            hidden_dim_processor,
            hidden_dim_node_encoder,
            num_layers_node_encoder,
            hidden_dim_edge_encoder,
            num_layers_edge_encoder,
            hidden_dim_node_decoder,
            num_layers_node_decoder,
            aggregation,
        )
        self.k = k
        self.PivotalNorm = torch.nn.LayerNorm(output_encode_dim)

    def knn_interpolate(
        self,
        x: torch.Tensor,
        pos_x: torch.Tensor,
        pos_y: torch.Tensor,
        batch_x: torch.Tensor = None,
        batch_y: torch.Tensor = None,
        k: int = 3,
        num_workers: int = 1,
    ):
        with torch.no_grad():
            assign_index = torch_cluster.knn(
                pos_x,
                pos_y,
                k,
                batch_x=batch_x,
                batch_y=batch_y,
                num_workers=num_workers,
            )
            y_idx, x_idx = assign_index[0], assign_index[1]
            diff = pos_x[x_idx] - pos_y[y_idx]
            squared_distance = (diff * diff).sum(dim=-1, keepdim=True)
            weights = 1.0 / torch.clamp(squared_distance, min=1e-16)

        y = torch_scatter.scatter(
            x[x_idx] * weights, y_idx, 0, dim_size=pos_y.size(0), reduce="sum"
        )
        y = y / torch_scatter.scatter(
            weights, y_idx, 0, dim_size=pos_y.size(0), reduce="sum"
        )

        return y.float(), x_idx, y_idx, weights

    def knn_interpolate_fast(
        self,
        x: torch.Tensor,
        pos_x: torch.Tensor,
        pos_y: torch.Tensor,
        weights: torch.Tensor,
        x_idx: torch.Tensor,
        y_idx: torch.Tensor,
    ):

        y = torch_scatter.scatter(
            x[x_idx] * weights, y_idx, 0, dim_size=pos_y.size(0), reduce="sum"
        )
        y = y / torch_scatter.scatter(
            weights, y_idx, 0, dim_size=pos_y.size(0), reduce="sum"
        )

        return y.float()

    def encode(self, x, edge_features, graph, position_mesh, position_pivotal):
        x = self.encoder_processor(x, edge_features, graph)
        x = self.PivotalNorm(x)
        nodes_index = torch.arange(graph.batch_size).to(x.device)
        batch_mesh = nodes_index.repeat_interleave(graph.batch_num_nodes())
        position_mesh_batch = position_mesh.repeat(graph.batch_size, 1)
        position_pivotal_batch = position_pivotal.repeat(graph.batch_size, 1)
        batch_pivotal = nodes_index.repeat_interleave(
            torch.tensor([len(position_pivotal)] * graph.batch_size).to(x.device)
        )

        if self.knn_every_step == True:
            # x, _, _, _ = self.knn_interpolate(x=x,pos_x=position_mesh,pos_y=position_pivotal)
            x, _, _, _ = self.knn_interpolate(
                x=x,
                pos_x=position_mesh_batch,
                pos_y=position_pivotal_batch,
                batch_x=batch_mesh,
                batch_y=batch_pivotal,
            )
        elif self.knn_every_step == False and self.knn_encoder_already == False:
            (
                x,
                self.x_idx_encode,
                self.y_idx_encode,
                self.weights_encode,
            ) = self.knn_interpolate(
                x=x, pos_x=position_mesh_batch, pos_y=position_pivotal_batch
            )
            self.knn_encoder_already = True
        elif self.knn_every_step == False and self.knn_encoder_already == True:
            x = self.knn_interpolate_fast(
                x=x,
                pos_x=position_mesh_batch,
                pos_y=position_pivotal_batch,
                weights=self.weights_encode,
                x_idx=self.x_idx_encode,
                y_idx=self.y_idx_encode,
            )

        return x

    def decode(self, x, edge_features, graph, position_mesh, position_pivotal):

        nodes_index = torch.arange(graph.batch_size).to(x.device)
        batch_mesh = nodes_index.repeat_interleave(graph.batch_num_nodes())
        position_mesh_batch = position_mesh.repeat(graph.batch_size, 1)
        position_pivotal_batch = position_pivotal.repeat(graph.batch_size, 1)
        batch_pivotal = nodes_index.repeat_interleave(
            torch.tensor([len(position_pivotal)] * graph.batch_size).to(x.device)
        )

        if self.knn_every_step == True:
            x, _, _, _ = self.knn_interpolate(
                x=x,
                pos_x=position_pivotal_batch,
                pos_y=position_mesh_batch,
                batch_x=batch_pivotal,
                batch_y=batch_mesh,
            )
        elif self.knn_every_step == False and self.knn_decoder_already == False:
            (
                x,
                self.x_idx_decode,
                self.y_idx_decode,
                self.weights_decode,
            ) = self.knn_interpolate(
                x=x, pos_x=position_pivotal_batch, pos_y=position_mesh_batch
            )
            self.knn_decoder_already = True
        elif self.knn_every_step == False and self.knn_decoder_already == True:
            x = self.knn_interpolate_fast(
                x=x,
                pos_x=position_pivotal_batch,
                pos_y=position_mesh_batch,
                weights=self.weights_decode,
                x_idx=self.x_idx_decode,
                y_idx=self.y_idx_decode,
            )

        x = self.decoder_processor(x, edge_features, graph)
        return x
