import torch
import torch.nn as nn
from pytorch3d.ops import sample_farthest_points, knn_points

class PositionalEncoding(nn.Module):

    def __init__(self, out_dim):
        super().__init__()
        # since we take in xyz, out in_dim is always three, 
        # but out_dim needs to be whatever will fight later,
        # for transformer layer... right?
        self.linear_1 = nn.Linear(3, out_dim)
        self.linear_2 = nn.Linear(out_dim, out_dim)
        self.relu = nn.ReLU()
    
    def forward(self, p_i, p_j):
        diff = p_i - p_j
        out = self.linear_1(diff)
        out = self.linear_2(out)
        out = self.relu(out)
        return out

def index_points(points, idx):
    """
    Input:
        points: input points data, [B, N, C]
        idx: sample index data, [B, S, [K]]
    Return:
        new_points:, indexed points data, [B, S, [K], C]
    """
    raw_size = idx.size()
    idx = idx.reshape(raw_size[0], -1)
    res = torch.gather(points, 1, idx[..., None].expand(-1, -1, points.size(-1)))
    return res.reshape(*raw_size, -1)

class PointTransformerLayer(nn.Module):
    
    def __init__(self, dim):
        super().__init__()
        self.gamma = nn.Linear(dim, dim)
        self.phi, self.psi, self.alpha = nn.Linear(dim, dim), nn.Linear(dim, dim), nn.Linear(dim, dim)
        self.softmax = nn.Softmax(dim=-1) # might need to change the dim, does not seem right here
        self.delta = PositionalEncoding(out_dim = dim)

    def forward(self, points_xyz, points_features, neighbours_xyz, neighbours_features):        
        # in the paper, they use the subtraction relation, and that is basically the beta function
        delta_out = self.delta(points_xyz.unsqueeze(-2), neighbours_xyz)
        beta = self.phi(points_features.unsqueeze(-2)) - self.psi(neighbours_features) + delta_out
        gamma_out = self.gamma(beta)
        rho_out = self.softmax(gamma_out) # wait does this actually work?
        alpha_out = self.alpha(neighbours_features) + delta_out
        out = torch.sum(rho_out * alpha_out, dim=2)
    
        return out

class PointTransformerBlock(nn.Module):

    def __init__(self, in_dim, out_dim, K=16):
        super().__init__()
        self.linear_1 = nn.Linear(in_dim, out_dim)
        self.point_transformer_layer = PointTransformerLayer(out_dim)
        self.linear_2 = nn.Linear(out_dim, in_dim)
        self.norm = nn.LayerNorm(out_dim)
        self.k = K

    def forward(self, points_xyz, points_features):
        residual = points_features.clone() # may contain xyz still
        # get the neighbours of each point
        distances = torch.cdist(points_xyz, points_xyz)
        _, indices = torch.topk(distances, self.k, largest=False)
        neighbours_xyz = index_points(points_xyz, indices)

        out = self.linear_1(points_features)
        out_neighbours_features = index_points(out, indices)

        out = self.point_transformer_layer(points_xyz, out, neighbours_xyz, out_neighbours_features)
        out = self.linear_2(out)
        out += residual

        return points_xyz, out 
    
class TransitionDownBlock(nn.Module):

    def __init__(self, npoints, in_dim, out_dim, K = 16):
        # honestly, TransitionDownBlock is just the set abstraction taken from PointNet++
        super().__init__()
        self.npoints = npoints # npoints // 4 at each stage
        self.linear = nn.Linear(in_dim, out_dim)
        self.batch_norm = nn.BatchNorm2d(out_dim) # got 4D data so need BatchNorm2D?
        self.relu = nn.ReLU()
        self.K = K

    def forward(self, points_xyz, points_features):
        sampled_points, _ = sample_farthest_points(points_xyz, K = self.npoints)
        _, indices, _ = knn_points(p1 = sampled_points, p2 = points_xyz, K = self.K)
        neighbours_features = index_points(points_features, indices)
        out = self.linear(neighbours_features)
        out = out.permute(0, 3, 1, 2) # change from B H W C to B C H W
        out = self.batch_norm(out)
        out = self.relu(out)
        out = torch.max(out, 3)[0] # B C H W -> B C H
        out = out.permute(0, 2, 1) # B C H -> B H C
        return sampled_points, out
    
class TransitionUpBlock(nn.Module):

    def __init__(self, in_dim, out_dim):
        super().__init__()
        self.linear_1a = nn.Linear(in_dim, out_dim) 
        self.linear_1b = nn.Linear(out_dim, out_dim)
        self.bn = nn.BatchNorm1d(out_dim) # looks to be 3D data so need to use BN1D
        self.relu = nn.ReLU()

    def forward(self, points_xyz, points_features, skipped_xyz, skipped_features):
        out = self.linear_1a(points_features)
        out = self.bn(out.permute(0, 2, 1))
        out = out.permute(0, 2, 1)
        out = self.relu(out)

        # trilinear interpolation
        interpolate_xyz, interpolate_features = interpolate(skipped_xyz, skipped_features, points_xyz, out)

        skipped = self.linear_1b(skipped_features)
        skipped = self.bn(skipped.permute(0,2,1))
        skipped = skipped.permute(0,2,1)
        skipped = self.relu(skipped)

        return interpolate_xyz, interpolate_features + skipped

def interpolate(xyz1: torch.Tensor, features1:torch.Tensor, xyz2:torch.Tensor, features2:torch.Tensor, k = 3) -> torch.Tensor:
    """Interpolate the features to the higher layer using inverse distance weighting

    Args:
        points1 (torch.Tensor): The l-1 layer output with d + C1 features. Not as many features, but more points
        points2 (torch.Tensor): The l layer output with d+C2 features. More features, but less points
        k (int, optional): k neighbours for interpolating the features. Defaults to 3.

    Returns:
        torch.Tensor: The interpolated features with the same number of points as points1, but with combined features of points1 and points2 (d + C1 + C2) 
    """

    distances = torch.cdist(xyz1, xyz2, p = 2) # B x N1 x N2
    topk_results = torch.topk(distances, k = k, dim = 2, largest = False) # call .values or .indices
    topk_weights = 1 / (topk_results.values + 1e-8) # add 1e-8 to prevent division by zero
    top_k_norm_weights = topk_weights / topk_weights.sum(dim = 2, keepdim = True)

    batch_indices = torch.arange(topk_results.indices.shape[0]).view(-1, 1).expand(-1, topk_results.indices.shape[1]*k).flatten()
    feat = features2[batch_indices, topk_results.indices.flatten(), :]
    feat = torch.mul(feat, top_k_norm_weights.flatten().unsqueeze(-1))
    feat = feat.reshape(-1, k, features2.shape[2])
    feat = feat.sum(dim=1)
    interpolated_features = feat.view(features1.shape[0], features1.shape[1], features2.shape[2])
  
    return xyz1, interpolated_features

if __name__ == "__main__":

    import numpy as np
    import torch

    np.random.seed(42)
    torch.manual_seed(42)    

    X = np.random.rand(5, 1000, 3).astype('float32')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    X = torch.tensor(X).to(device)