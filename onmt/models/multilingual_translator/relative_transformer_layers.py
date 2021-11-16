import torch
import torch.nn as nn
import torch.nn.functional as F
import onmt

from onmt.models.transformer_layers import PrePostProcessing
from onmt.modules.optimized.encdec_attention import EncdecMultiheadAttn
from onmt.modules.optimized.relative_self_attention import RelativeSelfMultiheadAttn
from onmt.modules.optimized.self_attention import SelfMultiheadAttn
from onmt.modules.optimized.feed_forward import PositionWiseFeedForward
from onmt.modules.multilingual_factorized.linear import MFWPositionWiseFeedForward
from onmt.modules.multilingual_factorized.encdec_attention import MFWEncdecMultiheadAttn
from onmt.modules.multilingual_factorized.relative_attention import MFWRelativeSelfMultiheadAttn
from onmt.modules.dropout import variational_dropout
from onmt.modules.identity import Identity
from onmt.modules.optimized.dropout_add import fused_dropout_add


def preprocessing(rezero, model_size, post_norm=False):
    sequence = ''

    if not rezero and not post_norm:
        sequence += 'n'

    return PrePostProcessing(model_size, 0.0, sequence=sequence)


def postprocessing(rezero, model_size, dropout, variational=False, post_norm=False,
                   dropout_residual=True):

    sequence = ''

    if dropout_residual:
        sequence += 'd'
        if rezero:
            sequence += 'z'
        else:
            sequence += 'a'
    if post_norm:
        sequence += 'n'

    return PrePostProcessing(model_size, dropout,
                             sequence=sequence,
                             variational=variational)


class RelativeTransformerEncoderLayer(nn.Module):
    def __init__(self, opt, death_rate=0.0, **kwargs):
        super(RelativeTransformerEncoderLayer, self).__init__()
        self.variational = opt.variational_dropout
        self.batch_ensemble = opt.batch_ensemble
        # self.multilingual_factorized_weights = opt.multilingual_factorized_weights
        self.death_rate = death_rate
        self.mfw = opt.multilingual_factorized_weights
        self.macaron = opt.macaron
        self.ffn_scale = 0.5 if self.macaron else 1
        self.dropout = opt.dropout
        self.residual_dropout = opt.residual_dropout if opt.residual_dropout >= 0 else opt.dropout
        self.ffn_dropout = opt.ffn_dropout if opt.ffn_dropout >= 0 else opt.dropout
        self.rezero = opt.rezero
        self.rotary_position_encoding = opt.rotary_position_encoding
        self.learnable_pos = opt.learnable_position_encoding
        self.stochastic_sublayer = opt.stochastic_sublayer
        self.post_norm = opt.post_norm

        if self.macaron:
            self.preprocess_mcr_ffn = preprocessing(opt.rezero, opt.model_size, self.post_norm)

            if self.mfw:
                self.mcr_feedforward = MFWPositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                                  variational=self.variational,
                                                                  n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                                  use_multiplicative=opt.mfw_multiplicative,
                                                                  no_bias=opt.mfw_no_bias,
                                                                  activation=opt.ffn_activation,
                                                                  glu=opt.ffn_glu)
            else:
                self.mcr_feedforward = PositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                               variational=self.variational,
                                                               activation=opt.ffn_activation,
                                                               glu=opt.ffn_glu,
                                                               dropout_residual=self.post_norm,
                                                               res_dropout=self.residual_dropout)

            self.postprocess_mcr_ffn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                                      self.variational, self.post_norm,
                                                      dropout_residual=not self.mcr_feedforward.dropout_residual)

        self.preprocess_attn = preprocessing(opt.rezero, opt.model_size, self.post_norm)
        self.postprocess_attn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                               self.variational, self.post_norm)
        self.preprocess_ffn = preprocessing(opt.rezero, opt.model_size, self.post_norm)

        d_head = opt.model_size // opt.n_heads

        if self.mfw:
            self.feedforward = MFWPositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                          variational=self.variational,
                                                          n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                          use_multiplicative=opt.mfw_multiplicative,
                                                          no_bias=opt.mfw_no_bias,
                                                          activation=opt.ffn_activation,
                                                          glu=opt.ffn_glu)

            self.multihead = MFWRelativeSelfMultiheadAttn(opt.model_size, opt.n_heads, opt.attn_dropout,
                                                          n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                          use_multiplicative=opt.mfw_multiplicative,
                                                          no_bias=opt.mfw_no_bias, )

        else:
            self.feedforward = PositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                       variational=self.variational,
                                                       activation=opt.ffn_activation,
                                                       glu=opt.ffn_glu,
                                                       dropout_residual=opt.post_norm,
                                                       res_dropout=self.residual_dropout)

            if not self.rotary_position_encoding:
                self.multihead = RelativeSelfMultiheadAttn(opt.model_size, opt.n_heads, opt.attn_dropout,
                                                           learnable_pos=self.learnable_pos,
                                                           max_pos=opt.max_pos_length)
            elif self.rotary_position_encoding:
                self.multihead = SelfMultiheadAttn(opt.model_size, opt.n_heads, opt.attn_dropout,
                                                   rotary_pos_enc=True)

        self.postprocess_ffn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                              self.variational, self.post_norm,
                                              dropout_residual=not self.feedforward.dropout_residual)

    def forward(self, input, pos_emb, attn_mask, src_lang=None,
                incremental=False, incremental_cache=None, mems=None):

        if incremental and incremental_cache is None:
            incremental_cache = dict()

        coin = True
        if self.training and self.death_rate > 0:
            coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:
            if self.macaron:
                out = self.mcr_feedforward(self.preprocess_mcr_ffn(input), src_lang)

                if self.training and self.death_rate > 0:
                    ffn_scale = self.ffn_scale / (1 - self.death_rate)
                else:
                    ffn_scale = self.ffn_scale

                input = self.postprocess_mcr_ffn(out * ffn_scale, input)
                # input = fused_dropout_add(out * ffn_scale, input,self.residual_dropout, self.training)

        if self.stochastic_sublayer:  # re-toss-coin
            if self.training and self.death_rate > 0:
                coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:
            query = self.preprocess_attn(input)

            if self.mfw:
                out, _ = self.multihead(query, pos_emb, src_lang, attn_mask, None, mems=mems,
                                        incremental=incremental, incremental_cache=incremental_cache)
            else:
                out, _ = self.multihead(query, pos_emb, attn_mask, None, mems=mems,
                                        incremental=incremental, incremental_cache=incremental_cache)

            # rescaling before residual
            if self.training and self.death_rate > 0:
                out = out / (1 - self.death_rate)

            input = self.postprocess_attn(out, input)
            # input = fused_dropout_add(out, input, self.residual_dropout, self.training)

        if self.stochastic_sublayer:  # re-toss-coin
            if self.training and self.death_rate > 0:
                coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:
            """ Feed forward layer 
                layernorm > ffn > dropout > residual
            """
            out = self.feedforward(self.preprocess_ffn(input), src_lang)

            # rescaling before residual
            if self.training and self.death_rate > 0:
                ffn_scale = self.ffn_scale / (1 - self.death_rate)
            else:
                ffn_scale = self.ffn_scale

            input = self.postprocess_ffn(out * ffn_scale, input)
            # input = fused_dropout_add(out * ffn_scale, input, self.residual_dropout, self.training)

        if incremental:
            return input, incremental_cache

        return input


class RelativeTransformerDecoderLayer(nn.Module):

    def __init__(self, opt, death_rate=0.0):
        super(RelativeTransformerDecoderLayer, self).__init__()
        self.ignore_source = opt.ignore_source
        self.variational = opt.variational_dropout
        self.death_rate = death_rate
        self.batch_ensemble = opt.batch_ensemble
        self.mfw = opt.multilingual_factorized_weights
        self.macaron = opt.macaron
        self.ffn_scale = 0.5 if self.macaron else 1
        self.dropout = opt.dropout
        self.residual_dropout = opt.residual_dropout if opt.residual_dropout >= 0 else opt.dropout
        self.ffn_dropout = opt.ffn_dropout if opt.ffn_dropout >= 0 else opt.dropout
        self.rezero = opt.rezero
        self.n_heads = opt.n_heads
        self.learnable_pos = opt.learnable_position_encoding
        self.rotary_position_encoding = opt.rotary_position_encoding
        self.stochastic_sublayer = opt.stochastic_sublayer
        self.post_norm = opt.post_norm

        if self.macaron:
            self.preprocess_mcr_ffn = preprocessing(opt.rezero, opt.model_size, self.post_norm)

            if self.mfw:
                self.mcr_feedforward = MFWPositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                                  variational=self.variational,
                                                                  n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                                  use_multiplicative=opt.mfw_multiplicative,
                                                                  no_bias=opt.mfw_no_bias,
                                                                  activation=opt.ffn_activation,
                                                                  glu=opt.ffn_glu)

            else:
                self.mcr_feedforward = PositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                               variational=self.variational,
                                                               activation=opt.ffn_activation,
                                                               glu=opt.ffn_glu,
                                                               dropout_residual=opt.post_norm,
                                                               res_dropout=self.residual_dropout)

            self.postprocess_mcr_ffn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                                      self.variational, self.post_norm,
                                                      dropout_residual=not self.mcr_feedforward.dropout_residual)

        self.preprocess_attn = preprocessing(opt.rezero, opt.model_size, self.post_norm)
        self.postprocess_attn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                               self.variational, self.post_norm)

        if not self.ignore_source:
            self.preprocess_src_attn = preprocessing(opt.rezero, opt.model_size, self.post_norm)
            self.postprocess_src_attn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                                       self.variational, self.post_norm)

            if not self.mfw:
                self.multihead_src = EncdecMultiheadAttn(opt.n_heads, opt.model_size, opt.attn_dropout)
            else:
                self.multihead_src = MFWEncdecMultiheadAttn(opt.n_heads, opt.model_size, opt.attn_dropout,
                                                            n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                            use_multiplicative=opt.mfw_multiplicative,
                                                            no_bias=opt.mfw_no_bias, )

        self.preprocess_ffn = preprocessing(opt.rezero, opt.model_size, self.post_norm)
        d_head = opt.model_size // opt.n_heads

        if self.mfw:
            self.feedforward = MFWPositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                          variational=self.variational,
                                                          n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                          use_multiplicative=opt.mfw_multiplicative,
                                                          no_bias=opt.mfw_no_bias,
                                                          activation=opt.ffn_activation,
                                                          glu=opt.ffn_glu)

            self.multihead_tgt = MFWRelativeSelfMultiheadAttn(opt.model_size, opt.n_heads, opt.attn_dropout,
                                                              n_languages=opt.n_languages, rank=opt.mfw_rank,
                                                              use_multiplicative=opt.mfw_multiplicative,
                                                              no_bias=opt.mfw_no_bias, )
        else:

            self.feedforward = PositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                       variational=self.variational,
                                                       activation=opt.ffn_activation,
                                                       glu=opt.ffn_glu,
                                                       dropout_residual=opt.post_norm,
                                                       res_dropout=self.residual_dropout)

            if self.rotary_position_encoding:
                self.multihead_tgt = SelfMultiheadAttn(opt.model_size, opt.n_heads, opt.attn_dropout,
                                                       rotary_pos_enc=True)
            else:
                self.multihead_tgt = RelativeSelfMultiheadAttn(opt.model_size, opt.n_heads, opt.attn_dropout,
                                                               learnable_pos=self.learnable_pos,
                                                               max_pos=opt.max_pos_length)

        self.postprocess_ffn = postprocessing(opt.rezero, opt.model_size, self.residual_dropout,
                                              self.variational, self.post_norm,
                                              dropout_residual=not self.feedforward.dropout_residual)

    def forward(self, input, context, pos_emb, mask_tgt, mask_src,
                src_lang=None, tgt_lang=None, pos_emb_src=None,
                incremental=False, incremental_cache=None, reuse_source=True, mems=None):

        """ Self attention layer
            layernorm > attn > dropout > residual
        """

        if incremental and incremental_cache is None:
            incremental_cache = dict()

        coin = True
        if self.training and self.death_rate > 0:
            coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:

            if self.macaron:
                out = self.mcr_feedforward(self.preprocess_mcr_ffn(input), src_lang)

                if self.training and self.death_rate > 0:
                    ffn_scale = self.ffn_scale / (1 - self.death_rate)
                else:
                    ffn_scale = self.ffn_scale

                # input = self.postprocess_mcr_ffn(out * ffn_scale, input)
                input = fused_dropout_add(out * ffn_scale, input, self.residual_dropout, self.training)

        if self.stochastic_sublayer:
            if self.training and self.death_rate > 0:
                coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:
            # input and context should be T x B x H
            if mems is not None and mems.size(0) > 0:
                mems = self.preprocess_attn(mems)
            else:
                mems = None

            query = self.preprocess_attn(input)

            if self.mfw:
                out, _ = self.multihead_tgt(query, pos_emb, tgt_lang, None, mask_tgt, mems=mems,
                                            incremental=incremental, incremental_cache=incremental_cache)
            else:
                out, _ = self.multihead_tgt(query, pos_emb, None, mask_tgt, mems=mems,
                                            incremental=incremental, incremental_cache=incremental_cache)

            # rescaling before residual
            if self.training and self.death_rate > 0:
                out = out / (1 - self.death_rate)

            # input = self.postprocess_attn(out, input)
            input = fused_dropout_add(out, input, self.residual_dropout, self.training)

        if self.stochastic_sublayer:
            if self.training and self.death_rate > 0:
                coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:

            """ Context Attention layer 
                layernorm > attn > dropout > residual
            """
            if not self.ignore_source:
                query = self.preprocess_src_attn(input)
                incremental_source = incremental and reuse_source

                if self.mfw:
                    out, coverage = self.multihead_src(query, context, context, src_lang, tgt_lang, mask_src,
                                                       incremental=incremental_source,
                                                       incremental_cache=incremental_cache)
                else:
                    out, coverage = self.multihead_src(query, context, context, mask_src,
                                                       rotary_pos_enc=self.rotary_position_encoding,
                                                       pos_emb_q=pos_emb,
                                                       pos_emb_k=pos_emb_src,
                                                       incremental=incremental_source,
                                                       incremental_cache=incremental_cache)

                # rescaling before residual
                if self.training and self.death_rate > 0:
                    out = out / (1 - self.death_rate)

                # input = self.postprocess_src_attn(out, input)
                input = fused_dropout_add(out, input, self.residual_dropout, self.training)

            else:
                coverage = None

        else:
            coverage = input.new_zeros(input.size(1), self.n_heads,
                                       input.size(0), context.size(0) if context is None else input.size(0))

        if self.stochastic_sublayer:
            if self.training and self.death_rate > 0:
                coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:
            """ Feed forward layer 
                layernorm > ffn > dropout > residual
            """
            out = self.feedforward(self.preprocess_ffn(input), tgt_lang)

            # rescaling before residual
            if self.training and self.death_rate > 0:
                ffn_scale = self.ffn_scale / (1 - self.death_rate)
            else:
                ffn_scale = self.ffn_scale

            # input = self.postprocess_ffn(out * ffn_scale, input)
            input = fused_dropout_add(out * ffn_scale, input, self.residual_dropout, self.training)

        if incremental_cache is None:
            return input, coverage
        else:
            return input, coverage, incremental_cache
