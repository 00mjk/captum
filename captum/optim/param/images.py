from typing import List, Union, Tuple
import numpy as np

import torch
import torch.nn as nn
import torchvision.models as models
import torch.nn.functional as F
from torchvision import transforms
import torchvision.transforms.functional as TF

from lucid.misc.io import load, save, show

# mean = [0.485, 0.456, 0.406]
# std = [0.229, 0.224, 0.225]

# normalize = transforms.Normalize(mean=mean, std=std)

# mean = torch.Tensor(mean)[None, :, None, None]
# std = torch.Tensor(std)[None, :, None, None]


# def denormalize(x: torch.Tensor):
#     return std * x + mean


def logit(p: torch.Tensor, epsilon=1e-6) -> torch.Tensor:
    p = torch.clamp(p, min=epsilon, max=1.0 - epsilon)
    assert p.min() >= 0 and p.max() < 1
    return torch.log(p / (1 - p))


# def jitter(x: torch.Tensor, pad_width=2, pad_value=0.5):
#     _, _, H, W = x.shape
#     y = F.pad(x, 4 * (pad_width,), value=pad_value)
#     idx, idy = np.random.randint(low=0, high=2 * pad_width, size=(2,))
#     return y[:, :, idx : idx + H, idy : idy + W]


# def color_correction():
#     S = np.asarray(
#         [[0.26, 0.09, 0.02], [0.27, 0.00, -0.05], [0.27, -0.09, 0.03]]
#     ).astype("float32")
#     C = S / np.max(np.linalg.norm(S, axis=0))
#     C = torch.Tensor(C)
#     return C.transpose(0, 1)


class ToRGB(nn.Module):
    """Transforms arbitrary channels to RGB. We use this to ensure our 
    image parameteriation itself can be decorrelated. So this goes between 
    the image parameterization and the normalization/sigmoid step.
    
    We offer two transforms: Karhunen-Loève (KLT) and I1I2I3.
    
    KLT corresponds to the empirically measured channel correlations on imagenet.
    I1I2I3 corresponds to an aproximation for natural images from Ohta et al.[0]

    [0] Y. Ohta, T. Kanade, and T. Sakai, "Color information for region segmentation," 
    Computer Graphics and Image Processing, vol. 13, no. 3, pp. 222–241, 1980
    https://www.sciencedirect.com/science/article/pii/0146664X80900477
    """

    @staticmethod
    def klt_transform():
        """Karhunen-Loève transform (KLT) measured on ImageNet"""
        KLT = [[0.26, 0.09, 0.02], [0.27, 0.00, -0.05], [0.27, -0.09, 0.03]]
        transform = np.asarray(KLT, dtype=np.float32)
        transform /= np.max(np.linalg.norm(transform, axis=0))
        return torch.as_tensor(transform)

    @staticmethod
    def i1i2i3_transform():
        i1i2i3_matrix = [
            [1 / 3, 1 / 3, 1 / 3],
            [1 / 2, 0, -1 / 2],
            [-1 / 4, 1 / 2, -1 / 4],
        ]
        return torch.Tensor(i1i2i3_matrix)

    def __init__(self, transform_name="klt"):
        super().__init__()

        if transform_name == "klt":
            self.register_buffer("transform", ToRGB.klt_transform())
        elif transform_name == "i1i2i3":
            self.register_buffer("transform", ToRGB.i1i2i3_transform())
        else:
            raise ValueError(f"transform_name has to be either 'klt' or 'i1i2i3'")

    def forward(self, x, inverse=False):
        assert x.dim() == 3

        # alpha channel is taken off...
        has_alpha = x.size("C") == 4
        if has_alpha:
            x, alpha_channel = x[:3], x[3:]
            assert x.dim() == alpha_channel.dim()  # ensure we "keep_dim"

        h, w = x.size("H"), x.size("W")
        flat = x.flatten(("H", "W"), "spatials")
        if inverse:
            correct = self.transform.t() @ flat
        else:
            correct = self.transform @ flat
        chw = correct.unflatten("spatials", (("H", h), ("W", w))).refine_names("C", ...)

        # ...alpha channel is concatenated on again.
        if has_alpha:
            chw = torch.cat([chw, alpha_channel], 0)

        return chw


# def model(layer):
#     net = models.googlenet(pretrained=True)
#     net.train(False)

#     def get_subnet_at_layer(lay_idx):
#         subnet = nn.Sequential(*list(net._modules.values())[: lay_idx + 1])
#         subnet
#         for p in subnet.parameters():
#             p.requires_grad = False
#         return subnet

#     subnet = get_subnet_at_layer(layer)
#     subnet.train(False)
#     return subnet


# def upsample():
#     upsample = torch.nn.Upsample(scale_factor=1.1, mode="bilinear", align_corners=True)

#     def up(x):
#         upsample.scale_factor = (
#             1 + np.random.randn(1)[0] / 50,
#             1 + np.random.randn(1)[0] / 50,
#         )
#         return upsample(x)

#     return up


class ImageTensor(torch.Tensor):
    pass


class ImageParameterization(torch.nn.Module):
    def set_image(self, x: torch.Tensor):
        ...


class FFTImage(ImageParameterization):
    """Parameterize an image using inverse real 2D FFT"""

    def __init__(self, size, channels=3):
        super().__init__()
        assert len(size) == 2
        self.size = size

        coeffs_shape = (channels, size[0], size[1] // 2 + 1, 2)
        random_coeffs = torch.randn(
            coeffs_shape
        )  # names=["C", "H_f", "W_f", "complex"]
        self.fourier_coeffs = nn.Parameter(random_coeffs / 50)

        frequencies = FFTImage.rfft2d_freqs(*size)
        scale = 1.0 / np.maximum(frequencies, 1.0 / max(*size))
        scale *= np.sqrt(size[0] * size[1])
        spectrum_scale = torch.Tensor(scale[None, :, :, None].astype(np.float32))
        self.register_buffer("spectrum_scale", spectrum_scale)

    @staticmethod
    def rfft2d_freqs(height, width):
        """Computes 2D spectrum frequencies."""
        f_y = np.fft.fftfreq(height)[:, None]
        # on odd input dimensions we need to keep one additional frequency
        add = 2 if width % 2 == 1 else 1
        f_x = np.fft.fftfreq(width)[: width // 2 + add]
        return np.sqrt(f_x * f_x + f_y * f_y)

    def set_image(self, correlated_image: torch.Tensor):
        coeffs = torch.rfft(correlated_image, signal_ndim=2)
        self.fourier_coeffs = coeffs / self.spectrum_scale

    def forward(self):
        h, w = self.size
        scaled_spectrum = self.fourier_coeffs * self.spectrum_scale
        output = torch.irfft(scaled_spectrum, signal_ndim=2)[:, :h, :w]
        return output.refine_names("C", "H", "W")


class PixelImage(ImageParameterization):
    def __init__(self, size=None, channels: int = 3, init: torch.Tensor = None):
        super().__init__()
        if init is None:
            assert size is not None and channels is not None
            init = torch.randn([channels, size[0], size[1]]) / 10 + 0.5
        else:
            assert init.shape[0] == 3
        self.image = nn.Parameter(init)

    def forward(self):
        return self.image

    def set_image(self, correlated_image: torch.Tensor):
        self.image = nn.Parameter(correlated_image)


class LaplacianImage(ImageParameterization):
    def __init__(self):
        super().__init__()
        power = 0.1
        X = []
        scaler = []
        for scale in [1, 2, 4, 8, 16, 32]:
            upsample = torch.nn.Upsample(scale_factor=scale, mode="nearest")
            x = torch.randn([1, 3, 224 // scale, 224 // scale]) / 10
            x = x.cuda()
            x = x * (scale ** power) / (32 ** power)
            x.requires_grad = True
            X.append(x)
            scaler.append(upsample)

        self.parameters = X
        self.scaler = scaler

    def forward(self):
        A = []
        for xi, upsamplei in zip(self.X, self.scaler):
            A.append(upsamplei(xi))
        return torch.sum(torch.cat(A), 0) + 0.5


class NaturalImage(ImageParameterization):
    r"""Outputs an optimizable input image.
    
    By convention, single images are CHW and float32s in [0,1]. 
    The underlying parameterization is decorrelated via a ToRGB transform.
    When used with the (default) FFT parameterization, this results in a fully 
    uncorrelated image parameterization. :-)
    
    If a model requires a normalization step, such as normalizing imagenet RGB values,
    or rescaling to [0,255], it has to perform that step inside its computation.
    For example, our GoogleNet factory function has a `transform_input=True` argument.
    """

    def __init__(self, size, channels=3, Parameterization=FFTImage):
        super().__init__()

        self.parameterization = Parameterization(size=size, channels=channels)
        self.decorrelate = ToRGB(transform_name="klt")

    def forward(self):
        image = self.parameterization()
        image = self.decorrelate(image)
        image = image.rename(None)  # TODO: the world is not yet ready
        return torch.sigmoid_(image)

    def set_image(self, image):
        logits = logit(image, epsilon=1e-4)
        correlated = self.decorrelate(logits, inverse=True)
        self.parameterization.set_image(correlated)

