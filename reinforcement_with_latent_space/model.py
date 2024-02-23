import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
from torch.distributions import Categorical, Distribution, AffineTransform, TransformedDistribution, SigmoidTransform
from torch.distributions.mixture_same_family import MixtureSameFamily
from torch.distributions.uniform import Uniform
import parameters as params
    
class VisionNetwork(nn.Module):
    def __init__(self, ):
        super(VisionNetwork, self).__init__()

        img_channels=params.vision_dim[0]
        img_height=params.vision_dim[1]
        img_width=params.vision_dim[2]
        self.device = params.device
        self.out_dim = params.vision_embedding_dim

        # Encoder Layers
        # In PyTorch, normalization is usually done as a preprocessing step, but it can be included in the model
        self.rescaling = lambda x: x / 255.0  
        self.conv1 = nn.Conv2d(img_channels, 32, kernel_size=8, stride=4, padding=2)  # 'same' padding in PyTorch needs calculation
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2, padding=1)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1)
        self.flatten = nn.Flatten()

        fc_input_size = 64 * 2 
        self.dense1 = nn.Linear(fc_input_size, 512)
        self.dense2 = nn.Linear(512, self.out_dim)
    
    def _spatial_softmax(self, pre_softmax):
        N, C, H, W = pre_softmax.shape
        pre_softmax = pre_softmax.view(N*C, H * W)
        softmax = F.softmax(pre_softmax, dim=1)
        softmax = softmax.view(N, C, H, W)

        # Create normalized meshgrid
        x_coords = torch.linspace(0, 1, W, device=self.device)
        y_coords = torch.linspace(0, 1, H, device=self.device)
        X, Y = torch.meshgrid(x_coords, y_coords, indexing='xy')
        image_coords = torch.stack([X, Y], dim=-1).to(self.device)  # [H, W, 2]

        image_coords = image_coords.unsqueeze(0)  # [1, H, W, 2]
        image_coords = image_coords.unsqueeze(0)  # [1, H, W, 2] -> [1, 1, H, W, 2]
        
        # Reshape softmax for broadcasting
        softmax = softmax.unsqueeze(-1)  # [N, C, H, W, 1]

        # Compute spatial soft argmax
        # This tensor represents the 'center of mass' for each channel of each feature map in the batch
        spatial_soft_argmax = torch.sum(softmax * image_coords, dim=[2, 3])  # [N, C, 2]
        return spatial_soft_argmax

    def forward(self, x):
        x = self.rescaling(x)
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        pre_softmax = F.relu(self.conv3(x))
        spatial_soft_argmax = self._spatial_softmax(pre_softmax)
        x = self.flatten(spatial_soft_argmax)
        x = F.relu(self.dense1(x))
        x = self.dense2(x)

        return x

        
class PlanRecognition(nn.Module):
    def __init__(self):
        super(PlanRecognition, self).__init__()
        self.layer_size=2048
        self.epsilon=1e-4
        self.in_dim = params.vision_embedding_dim + params.proprioception_dim
        self.latent_dim = params.latent_dim

        # Encoder Layers
        self.lstm1 = nn.LSTM(self.in_dim, self.layer_size, batch_first=True, bidirectional=True)
        self.lstm2 = nn.LSTM(2 * self.layer_size, self.layer_size, batch_first=True, bidirectional=True)
        self.mu = nn.Linear(2 * self.layer_size, self.latent_dim)
        self.sigma = nn.Linear(2 * self.layer_size, self.latent_dim)

    def latent_normal(self, mu, sigma):
        dist = Normal(loc=mu, scale=sigma)
        return dist

    def forward(self, x):

        # LSTM Layers
        x, _ = self.lstm1(x)
        x, _ = self.lstm2(x)

        # Latent variable
        mu = self.mu(x)
        sigma = F.softplus(self.sigma(x+self.epsilon))
        dist = self.latent_normal(mu, sigma)

        return dist

    
class PlanProposal(nn.Module):
    def __init__(self):
        super(PlanProposal, self).__init__()
        self.layer_size=2048
        self.epsilon=1e-4

        # Encoder Layers
        self.in_features = 2*params.vision_embedding_dim + params.proprioception_dim
        self.latent_dim = params.latent_dim

        self.fc1 = nn.Linear(self.in_features, self.layer_size)
        self.fc2 = nn.Linear(self.layer_size, self.layer_size)
        self.fc3 = nn.Linear(self.layer_size, self.layer_size)
        self.fc4 = nn.Linear(self.layer_size, self.layer_size)
        self.fc5 = nn.Linear(self.layer_size, self.latent_dim)

    def latent_normal(self, mu, sigma):
        dist = Normal(loc=mu, scale=sigma)
        return dist

    def forward(self, vision_embeded, proprioception, goal_embeded):
        """
        x: (bs, input_size) -> input_size: goal (vision only) + current (visuo-proprio)
        """
        x = torch.cat([vision_embeded, proprioception, goal_embeded], dim=-1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))
        x = F.relu(self.fc4(x))
        mu = self.fc5(x)
        sigma = F.softplus(self.fc5(x+self.epsilon))
        dist = self.latent_normal(mu, sigma)

        return dist


class LogisticMixture(Distribution):
    def __init__(self, weightings, mu, scale, qbits):
        """
        Initializes the logistic mixture model.
        :param weightings: The logits for the categorical distribution.
        :param mu: The means of the logistic distributions.
        :param scale: The scales of the logistic distributions.
        :param qbits: Number of quantization bits.
        """
        super(LogisticMixture, self).__init__()
        arg_constraints = {}
        
        # Create a uniform distribution as the base for the logistic transformation
        base_distributions = Uniform(torch.zeros_like(mu), torch.ones_like(mu))
        
        # Apply the logistic transformation to the uniform base
        # Logistic(x; mu, scale) = mu + scale * log(x / (1-x))
        # Define transforms: Inverse of sigmoid followed by an affine transformation
        transforms = [SigmoidTransform().inv, AffineTransform(loc=mu, scale=scale)]

        # Create the transformed distribution representing the logistic distribution
        logistic_distributions = TransformedDistribution(base_distributions, transforms)
        
        # Create the mixture distribution
        self.mixture_dist = MixtureSameFamily(
            mixture_distribution=Categorical(logits=weightings),
            component_distribution=logistic_distributions,
        )
               
    def sample(self, sample_shape=torch.Size()):
        """
        Samples from the logistic mixture model.
        """
        x = self.mixture_dist.sample(sample_shape)
        x = torch.clamp(x, -1, 1)
        return x

    def log_prob(self, value):
        log_prob = self.mixture_dist.log_prob(value)
        return log_prob
    
class Actor(nn.Module):
  def __init__(self, layer_size=1024, epsilon=1e-4):
    super(Actor, self).__init__()
    self.epsilon = epsilon
    self.in_dim = 2*params.vision_embedding_dim + params.latent_dim + params.proprioception_dim
    self.action_dim = params.action_dim
    self.num_distribs = params.num_distribs
    self.device = params.device
    self.qbits = params.qbits

    self.lstm1 = nn.LSTM(input_size=self.in_dim,
                          hidden_size=layer_size, batch_first=True)
    self.lstm2 = nn.LSTM(input_size=layer_size, hidden_size=layer_size, batch_first=True)

    self.alpha = nn.Linear(layer_size, self.action_dim * self.num_distribs)
    self.mu = nn.Linear(layer_size, self.action_dim * self.num_distribs)
    self.sigma = nn.Linear(layer_size, self.action_dim * self.num_distribs)

  def forward(self, vision_embeded, proprioception, latent, goal_embeded):

    x = torch.cat([vision_embeded, proprioception, latent, goal_embeded], dim=-1)

    x, _ = self.lstm1(x)
    x, _ = self.lstm2(x)

    weightings = self.alpha(x).view(-1, self.action_dim, self.num_distribs)
    mu = self.mu(x).view(-1, self.action_dim, self.num_distribs)
    scale = torch.nn.functional.softplus(self.sigma(x + self.epsilon)).view(-1, self.action_dim, self.num_distribs)
    logistic_mixture = LogisticMixture(weightings, mu, scale, self.qbits)

    return logistic_mixture

class Critic(nn.Module):
    def __init__(self, layer_size=1024):
        super(Critic, self).__init__()
        self.in_features = params.vision_embedding_dim + params.proprioception_dim + params.action_dim
        # Fully Connected Layers
        self.fc1 = nn.Linear(self.in_features, layer_size)
        self.fc2 = nn.Linear(layer_size, layer_size)
        self.fc3 = nn.Linear(layer_size, layer_size)
        self.fc4 = nn.Linear(layer_size, layer_size)
        self.fc5 = nn.Linear(layer_size, 1)

    def forward(self, vision_embed, proprioception, action):

        x = torch.cat([vision_embed, proprioception, action], dim=-1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))
        q_value = F.relu(self.fc4(x))

        return q_value
    

        

 
