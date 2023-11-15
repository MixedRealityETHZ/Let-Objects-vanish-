# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/usr/bin/env python
"""
get_plane.py
"""
from __future__ import annotations

import random
import socket
import traceback
from datetime import timedelta
from typing import Any, Callable, Literal, Optional

import numpy as np
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import tyro
import yaml

from nerfstudio.configs.config_utils import convert_markup_to_ansi
from nerfstudio.configs.method_configs import AnnotatedBaseConfigUnion
from nerfstudio.engine.trainer import TrainerConfig
from nerfstudio.utils import comms, profiler
from nerfstudio.utils.rich_utils import CONSOLE
from sklearn.linear_model import LinearRegression
import matplotlib.pyplot as plt
import dataclasses
import functools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Literal, Optional, Tuple, Type, cast

import torch
from nerfstudio.configs.experiment_config import ExperimentConfig
from nerfstudio.data.datamanagers.base_datamanager import VanillaDataManager
from nerfstudio.engine.callbacks import TrainingCallback, TrainingCallbackAttributes, TrainingCallbackLocation
from nerfstudio.engine.optimizers import Optimizers
from nerfstudio.pipelines.base_pipeline import VanillaPipeline
from nerfstudio.utils import profiler, writer
from nerfstudio.utils.decorators import check_eval_enabled, check_main_thread, check_viewer_enabled
from nerfstudio.utils.misc import step_check
from nerfstudio.utils.rich_utils import CONSOLE
from nerfstudio.utils.writer import EventName, TimeWriter
from nerfstudio.viewer.server.viewer_state import ViewerState
from nerfstudio.viewer_beta.viewer import Viewer as ViewerBetaState
from rich import box, style
from rich.panel import Panel
from rich.table import Table
from torch.cuda.amp.grad_scaler import GradScaler
from sklearn.linear_model import LinearRegression
import numpy as np

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

DEFAULT_TIMEOUT = timedelta(minutes=30)

# speedup for when input size to model doesn't change (much)
torch.backends.cudnn.benchmark = True  # type: ignore


def _find_free_port() -> str:
    """Finds a free port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port

def launch(
    main_func: Callable,
    num_devices_per_machine: int,
    num_machines: int = 1,
    machine_rank: int = 0,
    dist_url: str = "auto",
    config: Optional[TrainerConfig] = None,
    timeout: timedelta = DEFAULT_TIMEOUT,
    device_type: Literal["cpu", "cuda", "mps"] = "cuda",
) -> None:
    """Function that spawns multiple processes to call on main_func

    Args:
        main_func (Callable): function that will be called by the distributed workers
        num_devices_per_machine (int): number of GPUs per machine
        num_machines (int, optional): total number of machines
        machine_rank (int, optional): rank of this machine.
        dist_url (str, optional): url to connect to for distributed jobs.
        config (TrainerConfig, optional): config file specifying training regimen.
        timeout (timedelta, optional): timeout of the distributed workers.
        device_type: type of device to use for training.
    """
    assert config is not None
    world_size = num_machines * num_devices_per_machine
    if world_size == 0:
        raise ValueError("world_size cannot be 0")
    elif world_size == 1:
        # uses one process
        try:
            main_func(config=config)
        except KeyboardInterrupt:
            # print the stack trace
            CONSOLE.print(traceback.format_exc())
        finally:
            profiler.flush_profiler(config.logging)

def plane_estimation(config: TrainerConfig):
    config.setup(local_rank=0, world_size=1)
    pipeline = config.pipeline.setup(device = "cuda")
    # optimizers = Optimizers(config.optimizers.copy(), pipeline.get_param_groups())
    grad_scaler = GradScaler(enabled=True)

    # load in the checkpoint
    load_dir = config.load_dir
    load_checkpoint = config.load_checkpoint
    if load_dir is not None:
        load_step = config.load_step
        if load_step is None:
            print("Loading latest Nerfstudio checkpoint from load_dir...")
            # NOTE: this is specific to the checkpoint name format
            load_step = sorted(int(x[x.find("-") + 1 : x.find(".")]) for x in os.listdir(load_dir))[-1]
        load_path: Path = load_dir / f"step-{load_step:09d}.ckpt"
        assert load_path.exists(), f"Checkpoint {load_path} does not exist"
        loaded_state = torch.load(load_path, map_location="cpu")
        _start_step = loaded_state["step"] + 1
        # load the checkpoints for pipeline, optimizers, and gradient scalar
        pipeline.load_pipeline(loaded_state["pipeline"], loaded_state["step"])
        # optimizers.load_optimizers(loaded_state["optimizers"])
        # if "schedulers" in loaded_state and config.load_scheduler:
        #     optimizers.load_schedulers(loaded_state["schedulers"])
        grad_scaler.load_state_dict(loaded_state["scalers"])
        CONSOLE.print(f"Done loading Nerfstudio checkpoint from {load_path}")
    elif load_checkpoint is not None:
        assert load_checkpoint.exists(), f"Checkpoint {load_checkpoint} does not exist"
        loaded_state = torch.load(load_checkpoint, map_location="cpu")
        _start_step = loaded_state["step"] + 1
        # load the checkpoints for pipeline, optimizers, and gradient scalar
        pipeline.load_pipeline(loaded_state["pipeline"], loaded_state["step"])
        #optimizers.load_optimizers(loaded_state["optimizers"])
        #if "schedulers" in loaded_state and config.load_scheduler:
            #optimizers.load_schedulers(loaded_state["schedulers"])
        grad_scaler.load_state_dict(loaded_state["scalers"])
        CONSOLE.print(f"Done loading Nerfstudio checkpoint from {load_checkpoint}")
    else:
        CONSOLE.print("No Nerfstudio checkpoint to load, so training from scratch.")

    output = pipeline.get_surface_detection(0,pipeline.datamanager.ray_bundle_surface_detection)
    num_image = len(pipeline.datamanager.expanded_cameras)
    assert num_image == len(output) , f"false length"

    transform_matrix = pipeline.datamanager.transform_matrix
    scale_factor = pipeline.datamanager.scale_factor
    world_xyz = []
    for i in range(num_image):
        
        fx = pipeline.datamanager.expanded_cameras[i].fx
        fy = pipeline.datamanager.expanded_cameras[i].fy
        cx = pipeline.datamanager.expanded_cameras[i].cx
        cy = pipeline.datamanager.expanded_cameras[i].cy
        c2w = pipeline.datamanager.expanded_cameras[i].camera_to_worlds
        depth = output[i].to(fx.device)
        y = pipeline.datamanager.ray_indices[i][1]
        x = pipeline.datamanager.ray_indices[i][2]
        # xyz in camera coordinates
        X = (x - cx) * depth / fx
        Y = (y - cy) * depth / fy
        Z = depth
        # Convert to world coordinates
        camera_xyz = torch.stack([X, Y, Z, torch.ones_like(X)], dim=-1)
        c2w = c2w.to(camera_xyz.device)
        #world_xyz.append((c2w @ camera_xyz.T).T[..., :3])
        world_coordinates = (c2w @ camera_xyz.T).T[..., :3]
        # Transform the world coordinates
        world_coordinates_homogeneous = torch.cat([world_coordinates, torch.ones(len(world_coordinates), 1)], dim=-1)
        transformed_world_coordinates = (transform_matrix.to(world_coordinates_homogeneous.device) @ world_coordinates_homogeneous.T).T[..., :3]
        scaled_transformed_world_coordinates = transformed_world_coordinates * scale_factor
        world_xyz.append(scaled_transformed_world_coordinates)
    
    # calculate the plane equation using linear regression
    # Flatten the world_xyz list and convert it to a numpy array
    world_xyz_np = np.concatenate([xyz.numpy() for xyz in world_xyz], axis=0)
    # Create a LinearRegression object
    reg = LinearRegression()

    # Fit the model to the data
    reg.fit(world_xyz_np[:, :2], world_xyz_np[:, 2])

    # The coefficients a, b are in reg.coef_, and the intercept d is in reg.intercept_
    a, b = reg.coef_
    d = reg.intercept_
    c = -1

    vertices = pipeline.datamanager.vertices
    print("Vertices of bbox\n")
    print(vertices)

    #uncomment this for visualization
    
    # Create a 3D plot
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    ax.set_title('bbox with transformation')
    ## Plot the points in world_xyz
    #for xyz in world_xyz:
    #    ax.scatter(xyz[:, 0], xyz[:, 1], xyz[:, 2])

    # Plot the plane
    xx, yy = np.meshgrid(range(-5, 5), range(-5, 5))
    zz = (-a * xx - b * yy - d) / c
    ax.plot_surface(xx, yy, zz, alpha=0.5)

    # plot the reference plane
    xx1, yy1 = np.meshgrid(range(-5, 5), range(-5, 5))
    zz1 = (0.7365434765815735 * xx1 + 0.14943844079971313 * yy1 + 0.01226318534463644)/(-1)
    ax.plot_surface(xx1, yy1, zz1, alpha=0.5, color = 'r')

    
    #print(vertices.shape) # 8 x 3
    #TODO: plot the vertices
    # Plot the vertices
    for vertex in vertices:
        ax.scatter(*vertex)

    # Plot the bbox
    
    plt.show()
    
    # The equation of the plane is `ax + by + cz + d = 0`
    CONSOLE.print(f"The equation of the plane is {a}x + {b}y + {c}z + {d} = 0")
def main(config: TrainerConfig) -> None:
    """Main function."""
    # config, pipeline, checkpoint_path, _ = eval_setup(self.load_config)
    # assert self.output_path.suffix == ".json"
    # if self.render_output_path is not None:
    #     self.render_output_path.mkdir(parents=True)
    # metrics_dict = pipeline.get_average_eval_image_metrics(output_path=self.render_output_path, get_std=True)
    # self.output_path.parent.mkdir(parents=True, exist_ok=True)
    # # Get the output and define the names to save to
    
    # # Save output to output file
    # self.output_path.write_text(json.dumps(benchmark_info, indent=2), "utf8")
    # CONSOLE.print(f"Saved results to: {self.output_path}")
    # config, pipeline, checkpoint_path, _ = eval_setup(self.load_config)     #setting up the models

    # camera_indices, pixel_coords = self.sample_pixels()
    # raybundles = pipeline.datamanager.train_dataset.cameras._generate_rays_from_coords(camera_indices=camera_indices,coords=pixel_coords,disable_distortion = True)
    # ## raybundles reshape
    # renderer_depth = DepthRenderer(method="median")
    # outputs[f"prop_depth_{i}"] = renderer_depth(weights=weights_list[i], ray_samples=ray_samples_list[i])

    if config.data:
        CONSOLE.log("Using --data alias for --data.pipeline.datamanager.data")
        config.pipeline.datamanager.data = config.data

    if config.prompt:
        CONSOLE.log("Using --prompt alias for --data.pipeline.model.prompt")
        config.pipeline.model.prompt = config.prompt

    if config.load_config:
        CONSOLE.log(f"Loading pre-set config from: {config.load_config}")
        config = yaml.load(config.load_config.read_text(), Loader=yaml.Loader)

    launch(
        main_func=plane_estimation,
        num_devices_per_machine=config.machine.num_devices,
        device_type=config.machine.device_type,
        num_machines=config.machine.num_machines,
        machine_rank=config.machine.machine_rank,
        dist_url=config.machine.dist_url,
        config=config,
    )





def entrypoint():
    """Entrypoint for use with pyproject scripts."""
    # Choose a base configuration and override values.
    tyro.extras.set_accent_color("bright_yellow")
    main(
        tyro.cli(
            AnnotatedBaseConfigUnion,
            description=convert_markup_to_ansi(__doc__),
        )
    )


if __name__ == "__main__":
    entrypoint()
