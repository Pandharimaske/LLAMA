import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelArgs:
    dim : int = 4096
    n_layers: int = 32
    n_heads:int = 32 # Number of heads for the queries
    n_kv_heads:Optional[int] = None # Number of heads for the K and V
    vocab_size:int = -1 # This will be set when we load the tokenizer
    multiple_of:int = 256
    ffn_dim_multiplier: Optional[float] = None
    norm_eps: float = 1e-5

    # Needed for KV cache
    max_batch_size: int = 32
    max_seq_len:int = 2048

    device: str = None

def precompute_theta_pos_frequencies(head_dim:int , seq_len: int , device: str , theta: float = 10000.0):
    # As written in the paper, the dimension of the embedding must be even
    assert head_dim % 2 == 0 , "Dimension must be divisible by 2"
    # Build the theta parameters
    # According to the formula theta_i = 10000 ^ (-2(i - 1) / dim) for i = [1,2,3,4 .... dim / 2]
    # Shape: (Head_dim / 2)
    theta_numerator = torch.arrange(0 , head_dim , 2).float()
    # Shape: (Head_Dim / 2)
    theta = 1.0 / (theta ** (theta_numerator / head_dim)).to(device)
    # Construct the postions (the 'm' parameter)
    # Shape: (Seq_Len)
    m = torch.arrange(seq_len , device = device)
    # Multiply each theta by each position using the outer product
    # Shape: (Seq_Len) outer_product* (Head_Dim / 2) -> (Seq_Len , Head_Dim / 2)
    freqs = torch.outer(m , theta).float()
    # We can compute complex numbers in the polar form c = R * exp(i * m * theta) , where R = 1 as follows:
    # (Seq_Len , Head_Dim / 2) -> Seq_Len , Head_Dim / 2)
    freqs_complex = torch.polar(torch.ones_like(freqs) , freqs)
    return freqs_complex

def apply_rotary_embeddings(x:torch.Tensor , freqs_complex: torch.Tensor , device:str):
    # (B , Seq_Len , H , Head_Dim) -> (B , Seq_Len , H , Head_Dim / 2)
    x_complex = torch.view_as_complex(x.float().reshape(*x.shape[:-1] , -1 , 2))
    # (Seq_Len , Head_Dim / 2) -> (1 , Seq_Len , 1 , Head_Dim / 2)
    freqs_complex = freqs_complex.unsqueeze(0).unsqueeze(2)
    # (B , Seq_Len , H , Head_Dim / 2) * (1 , Seq_Len , 1 , Head_Dim / 2) => (B , Seq_Len , H , Head_Dim / 2)
    x_rotated = x_complex * freqs_complex
    # (B, Seq_Len, H, Head Dim / 2) -> (B, Seq_Len, H, Head Dim / 2, 2)
    x_out = torch.view_as_real(x_rotated)
    # (B, Seq_Len, H,Head_Dim / 2, 2) -> (B, Seq_Len, H, Head_Dim)
    x_out = x_out.reshape(*x.shape)
    return x_out.type_as(x).to(device)

def repeat_kv(x : torch.Tensor , n_rep:int) -> torch.Tensor:
    batch_size , seq_len , n_kv_heads , head_dim = x.shape
    if n_rep == 1:
        return x
    return (
        # (B , Seq_Len , N_KV_Heads , 1 , Head_dim)
        x[: , : , : , None , :]
        .expand(batch_size , seq_len , n_kv_heads , n_rep , head_dim)
        .reshape(batch_size , seq_len , n_kv_heads * n_rep , head_dim)
    )


class RMSNorm(nn.Module):

    def __init__(self , dim: int , eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        # The gamma parameter
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self , x:torch.Tensor):
        # (B , Seq_Len , Dim)
        return x * torch.rsqrt(x.pow(2).mean(-1 , keepdim = True) + self.eps)

    def forward(self , x:torch.Tensor):
        # (Dim) * (B , Seq_Len , Dim) => (B , Seq_Len , Dim)
        # rsqrt = 1 / sqrt(x)
        return self.weight * self._norm(x.float()).type_as(x)
    
class SelfAttention(nn.Module):

    def __init__(self , args:ModelArgs):
        super().__init__()

        # Indicates the number of heads for the key and values
        self.n_kv_heads = args.n_heads if args.n_kv_heads is None else args.n_kv_heads
        # Indicates the number of heads for queries
        self.n_heads_q = args.n_heads
        # Indicates how many times the head of Keys and Values should be repeated to match the head of the Queries
        self.n_rep = self.n_heads_q // self.n_kv_heads
        # Indicates the dimension of each head
        self.head_dim = args.dim // args.n_heads

        self.wq = nn.Linear(args.dim , args.n_heads * self.head_dim , bias = False)
        self.wk = nn.Linear(args.dim , self.n_kv_heads * self.head_dim , bias = False)
        self.wv = nn.Linear(args.dim , self.n_kv_heads * self.head_dim , bias = False)
        self.wo = nn.Linear(args.n_heads * self.head_dim , args.dim , bias = False)

        self.cache_k = torch.zeros((args.max_batch_size , args.max_seq_len , self.n_kv_heads , self.head_dim))
        self.cache_v = torch.zeros((args.max_batch_size , args.max_seq_len , self.n_kv_heads , self.head_dim))

    def forward(self , x: torch.Tensor , start_pos:int , freqs_complex: torch.Tensor):
        batch_size , seq_len , _ = x.shape # (B , 1 , Dim)

        # Apply the Wq , Wk ,Wv matrices to queris , key and values
        # (B , 1 , Dim) -> (B , 1 , H_Q * Head_Dim)
        xq = self.wq(x)
        # (B , 1 , Dim) -> (B , 1 , H_KV * Head_Dim)
        xk = self.wk(x)
        xv = self.wv(x)

        # (B , 1 , H_Q * Head_Dim) --> (B , 1 , H_Q , Head_Dim)
        xq = xq.view(batch_size , seq_len , self.n_heads_q , self.head_dim)
        # (B , 1 , H_KV * Head_Dim) --> (B , 1 , H_KV , Head_Dim)
        xk = xk.view(batch_size , seq_len , self.n_kv_heads , self.head_dim)
        xv = xv.view(batch_size , seq_len , self.n_kv_heads , self.head_dim)

        # Does not change the shape of the tensors
        xq = apply_rotary_embeddings(xq , freqs_complex , device=x.device)
        xk = apply_rotary_embeddings(xk , freqs_complex , device=x.device)

        # Replace the entry in the cache for this token
        self.cache_k[:batch_size , start_pos: start_pos + seq_len] = xk
        self.cache_v[:batch_size , start_pos : start_pos + seq_len] = xv

        # Retrieve all the cached keys and values so far
        # (B , Seq_Len_KV , H_KV , Head_Dim)
        keys = self.cache_k[:batch_size , 0: start_pos + seq_len]
        values = self.cache_v[:batch_size , 0: start_pos + seq_len]

        # Repeate the heads of the K and V to reach the number of heads of the queries
        keys = repeat_kv(keys , self.n_rep)
        values = repeat_kv(values , self.n_rep)

        # (B , 1 , H_Q , Head_Dim) --> (B , H_Q , 1 , Head_Dim)
        xq = xq.transpose(1 , 2)
        keys = keys.transpose(1 , 2)
        values = values.transpose(1 , 2)

        # (B , H_Q , 1 , Head_Dim) @ (B , H_Q , Head_Dim , Seq_Len_KV) --> (B , H_Q , 1 , Seq_Len_KV)
        scores = torch.matmul(xq , keys.transpose(2 , 3)) / math.sqrt(self.head_dim)
        scores = F.softmax(scores.float() , dim = -1).type_as(xq)

        # (B , H_Q , 1 , Seq_Len) @ (B , H_Q , Seq_Len_KV , Head_Dim) ==> (B , H_Q , 1 , Head_Dim)
        output = torch.matmul(scores , values)

        # (B , H_Q , 1 , Head_Dim) -> (B , 1 , H_Q , Head_Dim) -> (B , 1 , Dim)
        output = (output.transpose(1 , 2).contiguous().view(batch_size , seq_len , -1))
        return self.wo(output) # (B , 1 , Dim) -> (B , 1 , Dim)



class FeedForward(nn.Module):

    def __init__(self , args: ModelArgs):
        super().__init__()

        hidden_dim = 4 * args.dim
        hidden_dim = int(2 * hidden_dim / 3)
        if args.ffn_dim_multiplier is not None:
            hidden_dim = int(args.ffn_dim_multiplier * hidden_dim)
        # Round the hidden_dim to the nearest multiple of the multiple_of parameter
        hidden_dim = args.multiple_of * ((hidden_dim + args.multiple_of - 1) // args.multiple_of)

        self.w1 = nn.Linear(args.dim , hidden_dim ,bias = False)
        self.w2 = nn.Linear(hidden_dim , args.dim ,bias = False)
        self.w3 = nn.Linear(args.dim , hidden_dim ,bias = False)

    def forward(self , x: torch.Tensor):
        swish = F.silu(self.w1(x))
        x_V = self.w3(x)
        x = swish * x_V
        x = self.w2(x)
        return x
    

class EncoderBlock(nn.Module):

    def __init__(self , args:ModelArgs):
        super().__init__()

        self.n_heads = args.n_heads
        self.dim = args.dim
        self.head_dim = args.dim // args.n_heads

        self.attention = SelfAttention(args)
        self.feed_forward = FeedForward(args)

        # Normalization BEFORE the self attention
        self.attention_norm = RMSNorm(args.dim , eps=args.norm_eps)
        # Normalization BEFORE the feed forward block
        self.ffn_norm = RMSNorm(args.dim , eps=args.norm_eps)

    def forward(self , x: torch.Tensor , start_pos: int , freqs_complex: torch.Tensor):
        # (B , Seq_Len , Dim) + (B , Seq_Len , Dim) --> (B , Seq_Len , Dim)
        h = x + self.attention.forward(self.attention_norm(x) , start_pos , freqs_complex)
        out = h + self.feed_forward.forward(self.ffn_norm(h))
        return out
    


      
class Transformer(nn.Module):

    def __init__(self , args: ModelArgs) -> None:
        super().__init__()

        assert args.vocab_size != -1, "Vocab size must be set"

        self.args = args
        self.vocab_size = args.vocab_size
        self.n_layers = args.n_layers
        self.tok_embeddings = nn.Embedding(self.vocab_size , args.dim)

        self.layers = nn.ModuleList()
        for _ in range(args.n_layers):
            self.layers.append(EncoderBlock(args))
        self.norm = RMSNorm(args.dim , eps = args.norm_eps)
        self.output = nn.Linear(args.dim , self.vocab_size , bias = False)

        self.freqs_complex = precompute_theta_pos_frequencies(self.arg.dim // self.args.n_layers , self.args.max_seq_len * 2 , device = self.args.device)

    def forward(self , tokens:torch.Tensor , start_pos: int):
        # (B , Seq_Len)
        batch_size , seq_len = tokens.shape
        assert seq_len == 1 , "Only one token at a time can be processed"

        # (B , Seq_Len) -> (B , Seq_Len , Dim)
        h = self.tok_embeddings(tokens)

        # Retrieve the pairs (m , theta) corresponding to the positions [start_pos , start_pos + seq_len]
        freqs_complex = self.freqs_complex[start_pos: start_pos + seq_len]

        # Consecutively apply all the encoder layers
        for layer in self.layers:
            h = layer(h , start_pos , freqs_complex)
        h = self.norm(h)
        output = self.output(h).float()
        return output
