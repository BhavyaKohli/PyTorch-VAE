import torch
import numpy as np

from models import BaseVAE
from torch import nn
from torch.nn import functional as F
from .types_ import *


class VanillaVAE(BaseVAE):
    def __init__(self,
                 in_channels: int,
                 latent_dim: int,
                 hidden_dims: List = None,
                 **kwargs) -> None:
        super(VanillaVAE, self).__init__()

        self.latent_dim = latent_dim
        if hidden_dims is None:
            self.hidden_dims = [32, 64, 128, 256, 512]
        else:
            self.hidden_dims = hidden_dims
        self.in_channels = in_channels

        self.built = False
    
    def build(self, dummy_batch, device):
        hidden_dims = self.hidden_dims
        input_shape = dummy_batch.shape

        modules = []
        # Build Encoder
        in_channels = self.in_channels
        for h_dim in hidden_dims:
            modules.append(
                nn.Sequential(
                    nn.Conv2d(in_channels, out_channels=h_dim,
                              kernel_size= 3, stride= 2, padding  = 1),
                    nn.BatchNorm2d(h_dim),
                    nn.LeakyReLU())
            )
            in_channels = h_dim

        self.encoder = nn.Sequential(*modules)
        dummy_encoder_result = self.encoder(dummy_batch)
        self.encoder_result_shape = dummy_encoder_result.shape[1:]

        self.fc_mu = nn.Linear(np.prod(self.encoder_result_shape), self.latent_dim)
        self.fc_var = nn.Linear(np.prod(self.encoder_result_shape), self.latent_dim)
        dummy_mu = self.fc_mu(torch.flatten(dummy_encoder_result, start_dim=1))

        # Build Decoder
        modules = []
        self.decoder_input = nn.Linear(self.latent_dim, np.prod(self.encoder_result_shape))

        hidden_dims_r = hidden_dims[::-1]

        for i in range(len(hidden_dims_r) - 1):
            modules.append(
                nn.Sequential(
                    nn.ConvTranspose2d(hidden_dims_r[i],
                                       hidden_dims_r[i + 1],
                                       kernel_size=3,
                                       stride = 2,
                                       padding=1,
                                       output_padding=1),
                    nn.BatchNorm2d(hidden_dims_r[i + 1]),
                    nn.LeakyReLU())
            )
        
        self.decoder = nn.Sequential(*modules)
        
        self.final_layer = nn.Sequential(
                            nn.ConvTranspose2d(hidden_dims_r[-1],
                                               hidden_dims_r[-1],
                                               kernel_size=3,
                                               stride=2,
                                               padding=1,
                                               output_padding=1),
                            nn.BatchNorm2d(hidden_dims_r[-1]),
                            nn.LeakyReLU()
        )

        dummy_decoder_result = self.final_layer(self.decoder(self.decoder_input(dummy_mu).view(-1, *self.encoder_result_shape)))
        decoder_result_shape = dummy_decoder_result.shape

        required_dim = input_shape[-1]
        current_dim = decoder_result_shape[-1]
        kernel = int(-((required_dim - 1) * 1 + 1 - 2*1 - current_dim) / 1 + 1)                     

        self.output_layer = nn.Conv2d(hidden_dims_r[-1], out_channels=self.in_channels,
                                      kernel_size=kernel, padding=1)
        self.built = True
        self = self.to(device)

    def encode(self, input: Tensor) -> List[Tensor]:
        """
        Encodes the input by passing through the encoder network
        and returns the latent codes.
        :param input: (Tensor) Input tensor to encoder [N x C x H x W]
        :return: (Tensor) List of latent codes
        """
        if not self.built: raise AttributeError("build model first using model.build") 
        result = self.encoder(input)
        result = torch.flatten(result, start_dim=1)

        # Split the result into mu and var components
        # of the latent Gaussian distribution
        mu = self.fc_mu(result)
        log_var = self.fc_var(result)

        return [mu, log_var]

    def decode(self, z: Tensor) -> Tensor:
        """
        Maps the given latent codes
        onto the image space.
        :param z: (Tensor) [B x D]
        :return: (Tensor) [B x C x H x W]
        """
        if not self.built: raise AttributeError("build model first using model.build") 
        result = self.decoder_input(z)
        result = result.view(-1, *self.encoder_result_shape)
        result = self.decoder(result)
        result = self.final_layer(result)
        result = self.output_layer(result)
        return result

    def reparameterize(self, mu: Tensor, logvar: Tensor) -> Tensor:
        """
        Reparameterization trick to sample from N(mu, var) from
        N(0,1).
        :param mu: (Tensor) Mean of the latent Gaussian [B x D]
        :param logvar: (Tensor) Standard deviation of the latent Gaussian [B x D]
        :return: (Tensor) [B x D]
        """
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return eps * std + mu

    def forward(self, input: Tensor, **kwargs) -> List[Tensor]:
        if not self.built: raise AttributeError("build model first using model.build") 
        mu, log_var = self.encode(input)
        z = self.reparameterize(mu, log_var)
        return  [self.decode(z), input, mu, log_var]

    def loss_function(self,
                      *args,
                      **kwargs) -> dict:
        """
        Computes the VAE loss function.
        KL(N(\mu, \sigma), N(0, 1)) = \log \frac{1}{\sigma} + \frac{\sigma^2 + \mu^2}{2} - \frac{1}{2}
        :param args:
        :param kwargs:
        :return:
        """
        if not self.built: raise AttributeError("build model first using model.build") 

        recons = args[0]
        input = args[1]
        mu = args[2]
        log_var = args[3]

        kld_weight = kwargs['M_N'] # Account for the minibatch samples from the dataset
        recons_loss = F.mse_loss(recons, input)
        # recons_loss = F.mse_loss(gops.gumbel_sinkhorn(recons[:,0])[:,None], input)


        kld_loss = torch.mean(-0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1), dim = 0)

        loss = recons_loss + kld_weight * kld_loss
        return {'loss': loss, 'Reconstruction_Loss':recons_loss.detach(), 'KLD':-kld_loss.detach()}

    def sample(self,
               num_samples:int,
               current_device: int, **kwargs) -> Tensor:
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :param current_device: (Int) Device to run the model
        :return: (Tensor)
        """
        z = torch.randn(num_samples,
                        self.latent_dim)

        z = z.to(current_device)

        samples = self.decode(z)
        return samples

    def generate(self, x: Tensor, **kwargs) -> Tensor:
        """
        Given an input image x, returns the reconstructed image
        :param x: (Tensor) [B x C x H x W]
        :return: (Tensor) [B x C x H x W]
        """

        return self.forward(x)[0]