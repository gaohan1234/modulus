import torch
import numpy as np
from dgl.dataloading import GraphDataLoader
from torch.cuda.amp import autocast, GradScaler
from torch.nn.parallel import DistributedDataParallel
import time, os
import wandb as wb

from modulus.models.mesh_reduced.temporal_model import Sequence_Model
from modulus.models.mesh_reduced.mesh_reduced import Mesh_Reduced
from modulus.datapipes.gnn.vortex_shedding_re300_1000_dataset import VortexSheddingRe300To1000Dataset, LatentDataset
from modulus.distributed.manager import DistributedManager
from modulus.launch.logging import (
    PythonLogger,
    initialize_wandb,
    RankZeroLoggingWrapper,
)
from modulus.launch.utils import load_checkpoint, save_checkpoint
from constants import Constants

C = Constants()
class Sequence_Trainer:
    def __init__(self, wb, dist, 
                 produce_latents = True,  
                 Encoder = None, 
		         position_mesh = None, 
		         position_pivotal = None, 
                 rank_zero_logger = None):
        self.dist = dist
        dataset_train = LatentDataset(
            split="train",
            produce_latents = produce_latents,
		    Encoder = Encoder, 
		    position_mesh = position_mesh, 
		    position_pivotal = position_pivotal,
            dist = dist
        )

        self.dataloader = GraphDataLoader(
            dataset_train,
            batch_size=1,
            shuffle=False,
            drop_last=False,
            pin_memory=True,
            use_ddp=dist.world_size > 1,
        )

        self.dataset_graph_train = VortexSheddingRe300To1000Dataset(
            name="vortex_shedding_train",
            split="train"
        )

      
        self.dataloader_graph = GraphDataLoader(
            self.dataset_graph_train,
            batch_size=C.batch_size,
            shuffle=False,
            drop_last=False,
            pin_memory=True,
            use_ddp=dist.world_size > 1,
        )
        self.model = Sequence_Model(C.sequence_dim, C.sequence_content_dim, dist)

        if C.jit:
            self.model = torch.jit.script(self.model).to(dist.device)
        else:
            self.model = self.model.to(dist.device)
        if C.watch_model and not C.jit and dist.rank == 0:
            wb.watch(self.model)
        # enable train mode
        self.model.eval()

        # instantiate loss, optimizer, and scheduler
        self.criterion = torch.nn.MSELoss()
        # instantiate loss, optimizer, and scheduler
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=C.lr)
        self.scheduler = torch.optim.lr_scheduler.LambdaLR(
            self.optimizer, lr_lambda=lambda epoch: C.lr_decay_rate**epoch
        )
        self.scaler = GradScaler()

        # load checkpoint
        if dist.world_size > 1:
            torch.distributed.barrier()
        self.epoch_init = load_checkpoint(
            os.path.join(C.ckpt_sequence_path, C.ckpt_sequence_name),
            models=self.model,
            optimizer=self.optimizer,
            scheduler=self.scheduler,
            scaler=self.scaler,
            device=dist.device,
        )
    
    def denormalize(self, sample):
        for j in range(sample.size()[0]):	
            sample[j] = self.dataset_graph_train.denormalize(sample[j], self.dataset_graph_train.node_stats["node_mean"].to(self.dist.device),  
            self.dataset_graph_train.node_stats["node_std"].to(self.dist.device))
        return sample
    
    @torch.no_grad()
    def sample(self, z0, context, ground_trueth, true_latent, encoder, graph, position_mesh, position_pivotal):
        self.model.eval()
        x_samples = []
        z0 = z0.to(self.dist.device)
        context = context.to(self.dist.device)
        z_samples = self.model.sample(z0, 399, context)
        for i in range(401):
            z_sample = z_samples[0, i]
            z_sample = z_sample.reshape(256, 3)

            x_sample = encoder.decode(z_sample, graph.edata["x"], graph,  position_mesh, position_pivotal)
            x_samples.append(x_sample.unsqueeze(0))
        x_samples = torch.cat(x_samples)
        # x_samples = self.denormalize(x_samples)
        # ground_trueth = self.denormalize(ground_trueth)
        
        loss_record_u = []
        loss_record_v = []
        loss_record_p = []
      


        for i in range(400):
            loss = self.criterion(ground_trueth[i+1:i+2,:,0], x_samples[i+1:i+2,:,0])          
            relative_error = loss/self.criterion(ground_trueth[i+1:i+2,:,0], ground_trueth[i+1:i+2,:,0]*0.0).detach()
            loss_record_u.append(relative_error)
        for i in range(400):
            loss = self.criterion(ground_trueth[i+1:i+2,:,1], x_samples[i+1:i+2,:,1])          
            relative_error = loss/self.criterion(ground_trueth[i+1:i+2,:,1], ground_trueth[i+1:i+2,:,1]*0.0).detach()
            loss_record_v.append(relative_error)
        for i in range(400):
            loss = self.criterion(ground_trueth[i+1:i+2,:,2], x_samples[i+1:i+2,:,2])          
            relative_error = loss/self.criterion(ground_trueth[i+1:i+2,:,2], ground_trueth[i+1:i+2,:,2]*0.0).detach()
            loss_record_p.append(relative_error)
        
        
        
        
        return x_samples, relative_error

    def forward(self, z, context = None):
        with autocast(enabled=C.amp):
            prediction  = self.model(z, context)
            loss = self.criterion(z[:,1:], prediction[:,:-1])
            relative_error = torch.sqrt(loss/self.criterion(z[:,1:], z[:,1:]*0.0)).detach()
            return loss, relative_error
        
    def train(self, z, context):
        z = z.to(self.dist.device)
        context = context.to(self.dist.device)
        self.optimizer.zero_grad()
        loss, relative_error = self.forward(z, context)
        self.backward(loss)
        self.scheduler.step()
        return loss, relative_error

    def backward(self, loss):
        # backward pass
        if C.amp:
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            loss.backward()
            self.optimizer.step()



if __name__ == "__main__":
    # initialize distributed manager
    DistributedManager.initialize()
    dist = DistributedManager()

    # save constants to JSON file
    if dist.rank == 0:
        os.makedirs(C.ckpt_sequence_path, exist_ok=True)
        with open(
            os.path.join(C.ckpt_sequence_path, C.ckpt_sequence_name.replace(".pt", ".json")), "w"
        ) as json_file:
            json_file.write(C.json(indent=4))

    # initialize loggers
    initialize_wandb(
        project="Modulus-Launch",
        entity="Modulus",
        name="Vortex_Shedding-Training",
        group="Vortex_Shedding-DDP-Group",
        mode=C.wandb_mode,
    )  # Wandb logger
    logger = PythonLogger("main")  # General python logger
    rank_zero_logger = RankZeroLoggingWrapper(logger, dist)  # Rank 0 logger
    logger.file_logging()

    position_mesh = torch.from_numpy(np.loadtxt(C.mesh_dir)).to(dist.device)
    position_pivotal = torch.from_numpy(np.loadtxt(C.pivotal_dir)).to(dist.device)
    #Load Graph Encoder
    Encoder = Mesh_Reduced(C.num_input_features, C.num_edge_features, C.num_output_features)
    Encoder = Encoder.to(dist.device)
    _ = load_checkpoint(
            os.path.join(C.ckpt_path, C.ckpt_name),
            models=Encoder,
            scaler =GradScaler(),
            device=dist.device
        )




    trainer = Sequence_Trainer(wb, dist, produce_latents=False, Encoder = Encoder, 
		         position_mesh = position_mesh, 
		         position_pivotal = position_pivotal,
                 rank_zero_logger = rank_zero_logger)
    start = time.time()
    rank_zero_logger.info("Testing started...")
    for graph in trainer.dataloader_graph:
        g = graph.to(dist.device)
        
        break
    ground_trueth = trainer.dataset_graph_train.solution_states


    
    i =0
    for lc in trainer.dataloader:
        ground = ground_trueth[0].to(dist.device)
       
        graph.ndata["x"]
        samples,relative_error = trainer.sample(lc[0][:,0:2], lc[1], ground, lc[0], Encoder, g, position_mesh, position_pivotal)
        i = i+1
     
    #avg_loss = loss_total/n_batch
    # rank_zero_logger.info(
    #         f"epoch: {epoch}, loss: {avg_loss:10.3e}, relative_error: {relative_error:10.3e},time per epoch: {(time.time()-start):10.3e}"
    #     )
    # wb.log({"loss": loss.detach().cpu()})

     
   
    rank_zero_logger.info("Sampling completed!")