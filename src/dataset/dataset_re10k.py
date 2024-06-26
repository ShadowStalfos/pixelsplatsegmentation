import json
from dataclasses import dataclass
from functools import cached_property
from io import BytesIO
from pathlib import Path
from typing import Literal

import numpy as np
import torch
import torchvision.transforms as tf
from einops import rearrange, repeat
from jaxtyping import Float, UInt8
from PIL import Image
from torch import Tensor
from torch.utils.data import IterableDataset

from ..geometry.projection import get_fov
from .dataset import DatasetCfgCommon
from .shims.augmentation_shim import apply_augmentation_shim
from .shims.crop_shim import apply_crop_shim
from .types import Stage
from .view_sampler import ViewSampler

import os
import matplotlib
matplotlib.use('Agg')  # Set the backend before importing pyplot
import matplotlib.pyplot as plt


@dataclass
class DatasetRE10kCfg(DatasetCfgCommon):
    name: Literal["re10k"]
    roots: list[Path]
    baseline_epsilon: float
    max_fov: float
    make_baseline_1: bool
    augment: bool


class DatasetRE10k(IterableDataset):
    cfg: DatasetRE10kCfg
    stage: Stage
    view_sampler: ViewSampler

    to_tensor: tf.ToTensor
    chunks: list[Path]
    near: float = 0.1
    far: float = 1000.0

    def __init__(
        self,
        cfg: DatasetRE10kCfg,
        stage: Stage,
        view_sampler: ViewSampler,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.stage = stage
        self.view_sampler = view_sampler
        self.to_tensor = tf.ToTensor()
        self.save_id = 0  # Initialize the index

        # Collect chunks.
        self.chunks = []
        for root in cfg.roots:
            root = root / self.data_stage
            root_chunks = sorted(
                [path for path in root.iterdir() if path.suffix == ".torch"]
            )
            self.chunks.extend(root_chunks)
        if self.cfg.overfit_to_scene is not None:
            chunk_path = self.index[self.cfg.overfit_to_scene]
            self.chunks = [chunk_path] * len(self.chunks)

    def shuffle(self, lst: list) -> list:
        indices = torch.randperm(len(lst))
        return [lst[x] for x in indices]

    def __iter__(self):
        # Chunks must be shuffled here (not inside __init__) for validation to show
        # random chunks.
        if self.stage in ("train", "val"):
            self.chunks = self.shuffle(self.chunks)

        # When testing, the data loaders alternate chunks.
        worker_info = torch.utils.data.get_worker_info()
        if self.stage == "test" and worker_info is not None:
            self.chunks = [
                chunk
                for chunk_index, chunk in enumerate(self.chunks)
                if chunk_index % worker_info.num_workers == worker_info.id
            ]

        for chunk_path in self.chunks:

            # Load the chunk.
            chunk = torch.load(chunk_path)

            if self.cfg.overfit_to_scene is not None:
                item = [x for x in chunk if x["key"] == self.cfg.overfit_to_scene]
                assert len(item) == 1
                chunk = item * len(chunk)

            if self.stage in ("train", "val"):
                chunk = self.shuffle(chunk)

            for example in chunk:
                object_masks = False
                if "objects" in example:
                    object_masks = True

                extrinsics, intrinsics = self.convert_poses(example["cameras"])
                scene = example["key"]

                try:
                    context_indices, target_indices = self.view_sampler.sample(
                        scene,
                        extrinsics,
                        intrinsics,
                    )
                except ValueError:
                    # Skip because the example doesn't have enough frames.
                    continue

                # Skip the example if the field of view is too wide.
                if (get_fov(intrinsics).rad2deg() > self.cfg.max_fov).any():
                    continue

                # Load the images.
                try:
                    context_images = [
                        example["images"][index.item()] for index in context_indices
                    ]
                    target_images = [
                        example["images"][index.item()] for index in target_indices
                    ]

                    context_images = self.convert_images(context_images)
                    target_images = self.convert_images(target_images)

                    if object_masks:
                        context_object_masks = [
                            example["objects"][index.item()] for index in context_indices
                        ]
                        target_object_masks = [
                            example["objects"][index.item()] for index in target_indices
                        ]

                        context_object_masks = self.convert_masks(context_object_masks)
                        target_object_masks = self.convert_masks(target_object_masks)

                        # self.save_image_and_mask(example, 'context', 'before', object_masks)
                        # self.save_image_and_mask(example, 'target', 'before', object_masks)

                except IndexError:
                    print(f"IndexError occurred: {e}")
                    continue

                # Skip the example if the images don't have the right shape.
                # context_image_invalid = context_images.shape[1:] != (3, 360, 640)
                # target_image_invalid = target_images.shape[1:] != (3, 360, 640)
                # if context_image_invalid or target_image_invalid:
                #     print(
                #         f"Skipped bad example {example['key']}. Context shape was "
                #         f"{context_images.shape} and target shape was "
                #         f"{target_images.shape}."
                #     )
                #     continue

                # Resize the world to make the baseline 1.
                context_extrinsics = extrinsics[context_indices]
                if context_extrinsics.shape[0] == 2 and self.cfg.make_baseline_1:
                    a, b = context_extrinsics[:, :3, 3]
                    scale = (a - b).norm()
                    if scale < self.cfg.baseline_epsilon:
                        print(
                            f"Skipped {scene} because of insufficient baseline "
                            f"{scale:.6f}"
                        )
                        continue
                    extrinsics[:, :3, 3] /= scale
                else:
                    scale = 1

                example = {
                    "context": {
                        "extrinsics": extrinsics[context_indices],
                        "intrinsics": intrinsics[context_indices],
                        "image": context_images,
                        "near": self.get_bound("near", len(context_indices)) / scale,
                        "far": self.get_bound("far", len(context_indices)) / scale,
                        "index": context_indices,
                    },
                    "target": {
                        "extrinsics": extrinsics[target_indices],
                        "intrinsics": intrinsics[target_indices],
                        "image": target_images,
                        "near": self.get_bound("near", len(target_indices)) / scale,
                        "far": self.get_bound("far", len(target_indices)) / scale,
                        "index": target_indices,
                    },
                    "scene": scene,
                }
                if object_masks:
                    example["context"]["objects"] = context_object_masks
                    example["target"]["objects"] = target_object_masks

                # self.save_image_and_mask(example, 'context', 'before', object_masks)
                # self.save_image_and_mask(example, 'target', 'before', object_masks)

                if self.stage == "train" and self.cfg.augment:
                    example = apply_augmentation_shim(example)
                example = apply_crop_shim(example, tuple(self.cfg.image_shape), imgs_nearest_neighbors=False)

                # Save images and masks after transformation
                # self.save_image_and_mask(example, 'context', 'after', object_masks)
                # self.save_image_and_mask(example, 'target', 'after', object_masks)

                # self.save_id += 1

                # if self.save_id == 20:
                #     raise Exception("Processing limit reached at index 1000.")

                yield example

    def convert_poses(
        self,
        poses: Float[Tensor, "batch 18"],
    ) -> tuple[
        Float[Tensor, "batch 4 4"],  # extrinsics
        Float[Tensor, "batch 3 3"],  # intrinsics
    ]:
        b, _ = poses.shape

        # Convert the intrinsics to a 3x3 normalized K matrix.
        intrinsics = torch.eye(3, dtype=torch.float32)
        intrinsics = repeat(intrinsics, "h w -> b h w", b=b).clone()
        fx, fy, cx, cy = poses[:, :4].T
        intrinsics[:, 0, 0] = fx
        intrinsics[:, 1, 1] = fy
        intrinsics[:, 0, 2] = cx
        intrinsics[:, 1, 2] = cy

        # Convert the extrinsics to a 4x4 OpenCV-style W2C matrix.
        w2c = repeat(torch.eye(4, dtype=torch.float32), "h w -> b h w", b=b).clone()
        w2c[:, :3] = rearrange(poses[:, 6:], "b (h w) -> b h w", h=3, w=4)
        return w2c.inverse(), intrinsics

    def convert_images(
        self,
        images: list[UInt8[Tensor, "..."]],
    ) -> Float[Tensor, "batch 3 height width"]:
        torch_images = []
        for image in images:
            image = Image.open(BytesIO(image.numpy().tobytes()))
            torch_images.append(self.to_tensor(image))
        return torch.stack(torch_images)

    def convert_masks(self, masks):
        torch_masks = []
        for mask in masks:
            mask = Image.open(BytesIO(mask.numpy().tobytes()))
            # Convert mask to a tensor: assumes that the mask is grayscale (1 channel)
            mask_tensor = torch.tensor(np.array(mask), dtype=torch.int64)  # Use int64 for categorical data
            torch_masks.append(mask_tensor.unsqueeze(0))  # Add a channel dimension if needed
        return torch.stack(torch_masks)

    def get_bound(
        self,
        bound: Literal["near", "far"],
        num_views: int,
    ) -> Float[Tensor, " view"]:
        value = torch.tensor(getattr(self, bound), dtype=torch.float32)
        return repeat(value, "-> v", v=num_views)

    @property
    def data_stage(self) -> Stage:
        if self.cfg.overfit_to_scene is not None:
            return "test"
        if self.stage == "val":
            return "test"
        return self.stage

    @cached_property
    def index(self) -> dict[str, Path]:
        merged_index = {}
        data_stages = [self.data_stage]
        if self.cfg.overfit_to_scene is not None:
            data_stages = ("test", "train")
        for data_stage in data_stages:
            for root in self.cfg.roots:
                # Load the root's index.
                with (root / data_stage / "index.json").open("r") as f:
                    index = json.load(f)
                index = {k: Path(root / data_stage / v) for k, v in index.items()}

                # The constituent datasets should have unique keys.
                assert not (set(merged_index.keys()) & set(index.keys()))

                # Merge the root's index into the main index.
                merged_index = {**merged_index, **index}
        return merged_index

    def __len__(self) -> int:
        return len(self.index.keys())

    def save_image_and_mask(self, example, stage, prefix, object_masks, save_dir='loaded_imgs_objs'):
        """Saves images and their corresponding masks to a directory."""
        os.makedirs(save_dir, exist_ok=True)  # Ensure the directory exists

        # Extract the relevant data from the example
        images = example[stage]['image']
        masks = example[stage].get('objects', None)  # Use None as default to check later

        for i, image_tensor in enumerate(images):
            fig, ax = plt.subplots(1, 2, figsize=(12, 6))

            # Image
            ax[0].imshow(image_tensor.permute(1, 2, 0))  # Convert CxHxW to HxWxC
            ax[0].set_title(f'{prefix} Image {i}')
            ax[0].axis('off')

            # Check if masks are available and align with current image index
            if object_masks:
                mask_tensor = masks[i]
                ax[1].imshow(mask_tensor.squeeze(), cmap='gray')  # Assume mask is single-channel
                ax[1].set_title(f'{prefix} Mask {i}')
            else:
                ax[1].text(0.5, 0.5, 'No mask available', horizontalalignment='center', verticalalignment='center')

            ax[1].axis('off')

            # Save the figure
            fig_filename = os.path.join(save_dir, f'{prefix}_{stage}_image_mask_{self.save_id}_{i}.png')
            fig.savefig(fig_filename)
            plt.close(fig)
            print(f"Saved figure to {fig_filename}")

