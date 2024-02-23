import copy
import torch
import numpy as np
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_

from model import VisionNetwork, PlanRecognition, PlanProposal, Actor, Critic
from prioritized_replay_buffer import PrioritizedReplayBuffer
from noise import OrnsteinUhlenbeckProcess
from utils import compute_loss, compute_regularisation_loss
import parameters as params
  
class AgentTrainer():
  def __init__(self, gamma=0.95, tau=0.01,):
    self.gamma = gamma
    self.lr = params.lr
    self.device = params.device
    self.action_dim = params.action_dim
    self.sequence_length = params.sequence_length
    self.grad_norm_clipping = params.grad_norm_clipping



    print(self.device)

    self.vision_network = VisionNetwork()
    self.plan_recognition = PlanRecognition()
    self.plan_proposal = PlanProposal()
    self.vision_network_optimizer = optim.Adam(self.vision_network.parameters(), lr=self.lr)
    self.plan_recognition_optimizer = optim.Adam(self.plan_recognition.parameters(), lr=self.lr)  
    self.plan_proposal_optimizer = optim.Adam(self.plan_proposal.parameters(), lr=self.lr)  

    self.actor = Actor()
    self.target_actor = Actor()
    self.actor_optimizer = optim.Adam(self.actor.parameters(),lr=self.lr)
    self.critic = Critic()
    self.target_critic = Critic()
    self.critic_optimizer = optim.Adam(self.critic.parameters(),lr=self.lr)

    self.pri_buffer = PrioritizedReplayBuffer(alpha=0.6, beta=0.4)
    self.noise = OrnsteinUhlenbeckProcess(size=self.action_dim)
    self.tau = tau

    # Wrap your models with DataParallel
    if torch.cuda.device_count() > 1:
      print("Using", torch.cuda.device_count(), "GPUs!")
      self.vision_network = torch.nn.DataParallel(self.vision_network)
      self.plan_recognition = torch.nn.DataParallel(self.plan_recognition)
      self.plan_proposal = torch.nn.DataParallel(self.plan_proposal)

      self.actor = torch.nn.DataParallel(self.actor)
      self.critic = torch.nn.DataParallel(self.critic)
      self.target_actor = torch.nn.DataParallel(self.target_actor)
      self.target_critic = torch.nn.DataParallel(self.target_critic)

    else:
      print("Using single GPU")
      self.vision_network = self.vision_network.to(self.device)
      self.plan_recognition = self.plan_recognition.to(self.device)
      self.plan_proposal = self.plan_proposal.to(self.device)

      self.actor = self.actor.to(self.device)
      self.critic = self.critic.to(self.device)
      self.target_actor = self.target_actor.to(self.device)
      self.target_critic = self.target_critic.to(self.device)


  def get_action(self, goal, vision, proprioception, greedy=False):

    goal_embeded = self.vision_network(goal)
    vision_embeded = self.vision_network(vision)

    proposal_dist = self.plan_proposal(vision_embeded, proprioception, goal_embeded)
    proposal_latent = proposal_dist.sample()
    action = self.actor(vision_embeded, proprioception, proposal_latent, goal_embeded)

    if not greedy:
        action += torch.tensor(self.noise.sample(),dtype=torch.float).cuda()
    return np.clip(action.detach().cpu().numpy(),-1.0,1.0)
  
  def pre_train(self, actions_label, video, proprioception):

    actions_label = torch.FloatTensor(actions_label).to(self.device)
    video = torch.FloatTensor(video).to(self.device)
    proprioception = torch.FloatTensor(proprioception).to(self.device)

    video_embeded = torch.stack([self.vision_network(video[:, i, :]) for i in range(self.sequence_length)], dim=1)
    goal_embeded = video_embeded[:, -1, :]
    vision_embeded = video_embeded[:, 0, :]

    # Combine CNN output with proprioception data
    combined = torch.cat([video_embeded, proprioception], dim=-1)
  
    recognition_dist = self.plan_recognition(combined)

    proposal_dist = self.plan_proposal(vision_embeded, proprioception[:, 0, :], goal_embeded)

    kl_loss = compute_regularisation_loss(recognition_dist, proposal_dist)
    normal_kl_loss = torch.mean(-0.5 * torch.sum(1 + proposal_dist.scale**2 - 
                                                 proposal_dist.loc**2 - torch.exp(proposal_dist.scale**2), dim=1), dim=0)

    proposal_latent = proposal_dist.sample()
    action_prob = self.actor(vision_embeded, proprioception[:, 0, :], proposal_latent, goal_embeded)

    recon_loss = compute_loss(actions_label, action_prob, self.sequence_length)

    loss = kl_loss + normal_kl_loss + recon_loss

    # Assuming the loss applies to all model components and they're all connected in the computational graph.
    self.vision_network.zero_grad()
    self.plan_recognition.zero_grad()
    self.plan_proposal.zero_grad()
    self.actor.zero_grad()

    # Only need to call backward once if all parts are connected and contribute to the loss.
    loss.backward()

    clip_grad_norm_(self.vision_network.parameters(), max_norm=self.grad_norm_clipping)
    clip_grad_norm_(self.plan_recognition.parameters(), max_norm=self.grad_norm_clipping)
    clip_grad_norm_(self.plan_proposal.parameters(), max_norm=self.grad_norm_clipping)
    clip_grad_norm_(self.actor.parameters(), max_norm=self.grad_norm_clipping)

    # Then step each optimizer
    self.vision_network_optimizer.step()
    self.plan_recognition_optimizer.step()
    self.plan_proposal_optimizer.step()
    self.actor_optimizer.step()

    return loss.item()
  
  @torch.no_grad()
  def td_target(self, goal, reward, vision, next_vision, next_proprioception, done):

    next_action = self.get_action(goal, vision)

    next_vision_embeded = self.vision_network(next_vision)
    next_q = self.target_critic(next_vision_embeded, next_proprioception, next_action)
    target = reward.unsqueeze(1) + self.gamma * next_q*(1.-done.unsqueeze(1))

    return target.float()

  def init_buffer(self, vision, proprioception, action, 
              reward, next_vision, next_proprioception, done):
    
    self.pri_buffer.store(vision, proprioception, action, reward, next_vision, next_proprioception, done)


  def fine_tune(self, goal):

    vision, proprioception, next_vision, next_proprioception, action, reward, done, weights, indices = self.pri_buffer.sample_batch()
   
    target_q = self.td_target(goal, reward, vision, next_vision, next_proprioception, done)

    vision_embeded = self.vision_network(vision)
    current_q = self.critic(vision_embeded, proprioception, action)

    critic_loss = torch.nn.MSELoss(current_q, target_q)
    """ Update priorities based on TD errors """
    td_errors = (target_q - current_q).t()          # Calculate the TD Errors

    self.pri_buffer.update_priorities(indices, td_errors.data.detach().cpu().numpy())

    action = self.get_action(goal, vision, greedy=False)
    pr = -self.critic(vision_embeded, proprioception, action).mean()
    pg = (action.pow(2)).mean()
    actor_loss = pr + pg*1e-3

    self.critic_optimizer.zero_grad()
    critic_loss.backward()
    clip_grad_norm_(self.critic.parameters(), max_norm=self.grad_norm_clipping)
    self.critic_optimizer.step()

    self.actor_optimizer.zero_grad()
    clip_grad_norm_(self.actor.parameters(), max_norm=self.grad_norm_clipping)
    actor_loss.backward()
    self.actor_optimizer.step()
  
    for target_param, param in zip(self.target_actor.parameters(), self.actor.parameters()):
      target_param.data.copy_(self.tau * param.data + (1.0 - self.tau) * target_param.data)
    for target_param, param in zip(self.target_critic.parameters(), self.critic.parameters()):
      target_param.data.copy_(self.tau * param.data + (1.0 - self.tau) * target_param.data)

  def save_checkpoint(self, filename):
      
    # Create a checkpoint dictionary containing the state dictionaries of all components
    checkpoint = {
        'vision_network_state_dict': self.vision_network.state_dict(),
        'plan_recognition_state_dict': self.plan_recognition.state_dict(),
        'plan_proposal_state_dict': self.plan_proposal.state_dict(),
        'actor_state_dict': self.actor.state_dict(),
        'target_actor_state_dict': self.target_actor.state_dict(),
        'critic_state_dict': self.critic.state_dict(),
        'target_critic_state_dict': self.target_critic.state_dict(),
        # Add any other states you wish to save
    }
    
    # Use torch.save to serialize and save the checkpoint dictionary
    torch.save(checkpoint, filename)
    print('Model saved')

  def load_checkpoint(self, filename):
      checkpoint = torch.load(filename)

      self.vision_network._load_from_state_dict(checkpoint['vision_network_state_dict'])
      self.plan_recognition._load_from_state_dict(checkpoint['plan_recognition_state_dict'])
      self.plan_proposal._load_from_state_dict(checkpoint['plan_proposal_state_dict'])

      self.actor.load_state_dict(checkpoint['actor_state_dict'])
      self.target_actor.load_state_dict(checkpoint['target_actor_state_dict'])

      self.critic.load_state_dict(checkpoint['critic_state_dict'])
      self.target_critic.load_state_dict(checkpoint['target_critic_state_dict'])

      print('Model loaded')