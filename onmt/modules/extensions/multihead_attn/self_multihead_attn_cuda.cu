#include <vector>
#include <math.h>
#include <iostream>
#include <cuda.h>
#include <cuda_runtime.h>
#include <cuda_fp16.h>
#include <cuda_profiler_api.h>

#include <ATen/ATen.h>
#include <ATen/cuda/CUDAContext.h>
#include <torch/extension.h>

#include "softmax_apex.h"

// symbol to be automatically resolved by PyTorch libs
// extern THCState *state;


int gemm_bias_lt(
    cublasLtHandle_t ltHandle,
    cublasOperation_t transa,
    cublasOperation_t transb,
    int m,
    int n,
    int k,
    const float *alpha, /* host pointer */
    at::Half* A,
    int lda,
    at::Half* B,
    int ldb,
    const float *beta, /* host pointer */
    at::Half* C,
    int ldc,
    void *workspace,
    size_t workspaceSize,
    cudaStream_t stream,
    bool use_bias,
    const void* bias,
    bool use_gelu,
    const void* gelu_in) {
  cublasStatus_t status = CUBLAS_STATUS_SUCCESS;

  cublasLtMatmulDescOpaque_t operationDesc = {};
  cublasLtMatrixLayoutOpaque_t Adesc = {}, Bdesc = {}, Cdesc = {};
  cublasLtMatmulPreferenceOpaque_t preference = {};

  int returnedResults                             = 0;
  cublasLtMatmulHeuristicResult_t heuristicResult = {};
  cublasLtEpilogue_t epilogue = CUBLASLT_EPILOGUE_DEFAULT;

  // Create operation descriptor; see cublasLtMatmulDescAttributes_t
  // for details about defaults; here we just set the transforms for
  // A and B.
  status = cublasLtMatmulDescInit(&operationDesc, CUBLAS_COMPUTE_32F, CUDA_R_32F);
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;
  status = cublasLtMatmulDescSetAttribute(&operationDesc, CUBLASLT_MATMUL_DESC_TRANSA, &transa, sizeof(transa));
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;
  status = cublasLtMatmulDescSetAttribute(&operationDesc, CUBLASLT_MATMUL_DESC_TRANSB, &transb, sizeof(transa));
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;

  if (use_gelu) {
    if (use_bias)
        epilogue = CUBLASLT_EPILOGUE_GELU_AUX_BIAS;
    else
        epilogue = CUBLASLT_EPILOGUE_GELU_AUX;
    status = cublasLtMatmulDescSetAttribute(&operationDesc, CUBLASLT_MATMUL_DESC_EPILOGUE_AUX_POINTER, &gelu_in, sizeof(gelu_in));
    status = cublasLtMatmulDescSetAttribute(&operationDesc, CUBLASLT_MATMUL_DESC_EPILOGUE_AUX_LD, &ldc, sizeof(ldc));
  }

  if (use_bias) {
    status = cublasLtMatmulDescSetAttribute(&operationDesc, CUBLASLT_MATMUL_DESC_BIAS_POINTER, &bias, sizeof(bias));
    if (status != CUBLAS_STATUS_SUCCESS) {
      goto CLEANUP;
    }
    if (!use_gelu)
      epilogue = CUBLASLT_EPILOGUE_BIAS;
  }

  status = cublasLtMatmulDescSetAttribute(&operationDesc, CUBLASLT_MATMUL_DESC_EPILOGUE, &epilogue, sizeof(epilogue));
  if (status != CUBLAS_STATUS_SUCCESS) {
    goto CLEANUP;
  }

  // Create matrix descriptors. Not setting any extra attributes.
  status = cublasLtMatrixLayoutInit(
    &Adesc, CUDA_R_16F, transa == CUBLAS_OP_N ? m : k, transa == CUBLAS_OP_N ? k : m, lda);
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;
  status = cublasLtMatrixLayoutInit(
    &Bdesc, CUDA_R_16F, transb == CUBLAS_OP_N ? k : n, transb == CUBLAS_OP_N ? n : k, ldb);
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;
  status = cublasLtMatrixLayoutInit(&Cdesc, CUDA_R_16F, m, n, ldc);
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;

  // Create preference handle; In general, extra attributes can be
  // used here to disable tensor ops or to make sure algo selected
  // will work with badly aligned A, B, C. However, for simplicity
  // here we assume A,B,C are always well aligned (e.g., directly
  // come from cudaMalloc)
  status = cublasLtMatmulPreferenceInit(&preference);
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;
  status = cublasLtMatmulPreferenceSetAttribute(
    &preference, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &workspaceSize, sizeof(workspaceSize));
  if (status != CUBLAS_STATUS_SUCCESS) goto CLEANUP;

  // We just need the best available heuristic to try and run matmul.
  // There is no guarantee that this will work. For example, if A is
  // badly aligned, you can request more (e.g. 32) algos and try to
  // run them one by one until something works.
  status = cublasLtMatmulAlgoGetHeuristic(
    ltHandle, &operationDesc, &Adesc, &Bdesc, &Cdesc, &Cdesc, &preference, 1, &heuristicResult, &returnedResults);
  if (status != CUBLAS_STATUS_SUCCESS)
    goto CLEANUP;

  if (returnedResults == 0) {
    status = CUBLAS_STATUS_NOT_SUPPORTED;
    goto CLEANUP;
  }
  status = cublasLtMatmul(ltHandle,
                          &operationDesc,
                          alpha,
                          A,
                          &Adesc,
                          B,
                          &Bdesc,
                          beta,
                          C,
                          &Cdesc,
                          C,
                          &Cdesc,
                          //&heuristicResult.algo,
                          NULL,
                          workspace,
                          workspaceSize,
                          stream);

CLEANUP:
  // Descriptors are no longer needed as all GPU work was already
  // enqueued.
  return status == CUBLAS_STATUS_SUCCESS ? 0 : 1;
}


namespace multihead_attn {
namespace self_bias_additive_mask {
namespace cublas_gemmex {

std::vector<torch::Tensor> fwd_cuda(
                               bool                 use_time_mask,
							   bool                 is_training,
                               int                  heads,
                               torch::Tensor const& inputs,
                               torch::Tensor const& input_weights,
                               torch::Tensor const& output_weights,
                               torch::Tensor const& input_biases,
                               torch::Tensor const& output_biases,
                               torch::Tensor const& pad_mask,
                               float                dropout_prob
                                   )
{
  const int   embed_dim      = inputs.size(2);
  const int   sequences      = inputs.size(1);
  const int   q_seq_len      = inputs.size(0);
  const int   k_seq_len      = q_seq_len;
  const int   batches        = sequences * q_seq_len;
  const int   head_dim       = embed_dim / heads;
  const int   output_lin_dim = 3 * embed_dim;
  const int   attn_batches   = heads * sequences;
  const int   lead_dim       = attn_batches * 3 * head_dim;
  const int   batch_stride   = 3 * head_dim;
  const int   dropout_elems  = attn_batches * q_seq_len * k_seq_len;
  const float alpha          = 1.0;
  const float beta_zero       = 0.0;
  const float beta_one           = 1.0;
  const float scale          = 1.0 / sqrt(static_cast<float>(head_dim));
  const half halpha = __float2half_rn(alpha);
  const half hbeta_zero = __float2half_rn(beta_zero);
  const half hbeta_one = __float2half_rn(beta_one);
  const half hscale = __float2half_rn(scale);

  // There is no reason to use more than one stream as every kernel is
  // sequentially dependent
  cublasHandle_t handle = at::cuda::getCurrentCUDABlasHandle();
  cudaStream_t   stream = at::cuda::getCurrentCUDAStream().stream();
  cublasSetStream(handle, stream);

  // 3 Intermediate Results + Output (Note: dropout intermediates are generated by ATen library code)
  auto act_options  = inputs.options().requires_grad(false);
  auto mask_options = act_options.dtype(torch::kUInt8);

  torch::Tensor input_lin_results = torch::empty({q_seq_len, sequences, output_lin_dim}, act_options);
  torch::Tensor attn_scores          = torch::empty({attn_batches, q_seq_len, k_seq_len},      act_options);
  torch::Tensor dropout_results   = torch::empty({attn_batches, q_seq_len, k_seq_len},   act_options);
  torch::Tensor softmax_results   = torch::empty({attn_batches, q_seq_len, k_seq_len},   act_options);
  torch::Tensor dropout_mask      = torch::empty({attn_batches, q_seq_len, k_seq_len},   mask_options);
  torch::Tensor matmul2_results   = torch::empty({q_seq_len, attn_batches, head_dim},    act_options);
  torch::Tensor outputs           = torch::empty_like(inputs, act_options);

  // Input Linear Results Pointers to Q, K, and V of interviewed activations
  void* q_lin_results_ptr   = static_cast<void*>(input_lin_results.data_ptr());
  void* k_lin_results_ptr   = static_cast<void*>(static_cast<half*>(input_lin_results.data_ptr()) + head_dim);
  void* v_lin_results_ptr   = static_cast<void*>(static_cast<half*>(input_lin_results.data_ptr()) + 2*head_dim);

  // Softmax Intermediate Result Ptr (used by Matmul1 -> Softmax)
  void* attn_scores_ptr = static_cast<void*>(attn_scores.data_ptr());
  void* dropout_results_ptr = static_cast<void*>(dropout_results.data_ptr());
  void* softmax_results_ptr = static_cast<void*>(softmax_results.data_ptr());

//  char a_layout_t{'t'};
//  char a_layout_n{'n'};
//  char b_layout_n{'n'};

  TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_TENSOR_OP_MATH));
  // Input Linear Fwd
  input_lin_results.copy_(input_biases);
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_T,
                             CUBLAS_OP_N,
                             output_lin_dim,
                             batches,
                             embed_dim,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(input_weights.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(inputs.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(&beta_one),
                             q_lin_results_ptr,
                             CUDA_R_16F,
                             output_lin_dim,
                             CUDA_R_32F,  // CUDA_R_32F or CUBLAS_COMPUTE_16F
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // MatMul1 of Dot-Product Attention Plus scaling by 1/Sqrt(head size)
  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_T,
                             CUBLAS_OP_N,
                             k_seq_len,  // m
                             q_seq_len,  // n
                             head_dim,   // k
                             static_cast<const void*>(&scale),
                             static_cast<const void*>(k_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(q_lin_results_ptr),
                             CUDA_R_16F,
                             lead_dim,  // attn_batches * 3 * head_dim
                             batch_stride,  // 3 * head_dim
                             static_cast<const void*>(&beta_zero),
                             static_cast<void*>(attn_scores_ptr), // C
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             attn_batches,  // batch = heads * bsz
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));
  if (use_time_mask){
    attn_scores.masked_fill_(pad_mask, -std::numeric_limits<float>::infinity());
  } else {
    attn_scores.view({sequences, heads, q_seq_len, k_seq_len}).masked_fill_(pad_mask,
                                                                          -std::numeric_limits<float>::infinity());
  }
  // Padded Softmax
  bool softmax_success = false;
//   if (is_training && dropout_prob > 0.0f) {

//       if (use_time_mask)    {
//           softmax_success = dispatch_additive_time_masked_softmax_dropout<half, half, float>(
//                                reinterpret_cast<half*>(dropout_results_ptr),
//                                (is_training) ? reinterpret_cast<uint8_t*>(dropout_mask.data_ptr<uint8_t>()) : nullptr,
//                                reinterpret_cast<const half*>(attn_scores_ptr),
//                                pad_mask,
//                                dropout_elems,
//                                k_seq_len,
//                                k_seq_len,
//                                attn_batches*q_seq_len,
//                                q_seq_len, // mod seq len
//                                1.0f-dropout_prob,
//                                stream);
//       } else {
//           // This function fuses softmax-dropout-pad (and dropout inplace)
//           softmax_success = dispatch_additive_masked_softmax_dropout<half, half, float>(
//                                reinterpret_cast<half*>(dropout_results_ptr),
//                                (is_training) ? reinterpret_cast<uint8_t*>(dropout_mask.data_ptr<uint8_t>()) : nullptr,
//                                reinterpret_cast<const half*>(attn_scores_ptr),
//                                pad_mask,
//                                dropout_elems,
//                                k_seq_len,
//                                k_seq_len,
//                                attn_batches*q_seq_len,
//                                attn_batches*q_seq_len/sequences,  // pad batch stride
//                                1.0f-dropout_prob,
//                                stream);
//       }
//   } else {
//       if (use_time_mask)    {
//           softmax_success = dispatch_additive_time_masked_softmax<half, half, float>(
//                                  reinterpret_cast<half*>(dropout_results_ptr), // this is actually softmax results, but making it consistent for the next function
//                                  reinterpret_cast<const half*>(attn_scores_ptr),
//                                  pad_mask,
//                                  k_seq_len,
//                                  k_seq_len,
//                                  attn_batches*q_seq_len,
//                                  q_seq_len,
//                                  stream);
//       } else  {
//           softmax_success = dispatch_additive_masked_softmax<half, half, float>(
//                                  reinterpret_cast<half*>(dropout_results_ptr), // this is actually softmax results, but making it consistent for the next function
//                                  reinterpret_cast<const half*>(attn_scores_ptr),
//                                  pad_mask,
//                                  k_seq_len,
//                                  k_seq_len,
//                                  attn_batches*q_seq_len,
//                                  attn_batches*q_seq_len/sequences,
//                                  stream);
//       }
//   }
  if (is_training && dropout_prob > 0.0f) {
      // This function fuses softmax-dropout-pad (and dropout inplace)
      softmax_success = dispatch_softmax_dropout<half, half, float>(
                           reinterpret_cast<half*>(dropout_results_ptr),
                           (is_training) ? reinterpret_cast<uint8_t*>(dropout_mask.data_ptr<uint8_t>()) : nullptr,
//                            reinterpret_cast<uint8_t*>(dropout_mask.data_ptr<uint8_t>()),
                           reinterpret_cast<const half*>(attn_scores_ptr),
//                            pad_mask,
      		               dropout_elems,
                           k_seq_len,
                           k_seq_len,
                           attn_batches*q_seq_len,
//                            attn_batches*q_seq_len/sequences, // pad batch strides
      		               1.0f-dropout_prob,
		                   stream);
  } else {
      softmax_success = dispatch_softmax<half, half, float>(
                             reinterpret_cast<half*>(dropout_results_ptr), // this is actually softmax results, but making it consistent for the next function
                             reinterpret_cast<const half*>(attn_scores_ptr),
//                              pad_mask,
                             k_seq_len,
                             k_seq_len,
                             attn_batches*q_seq_len,
//                              attn_batches*q_seq_len/sequences,
                             stream);  // pad batch strides
  }

  assert(softmax_success);

  // Matmul2
  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             head_dim,    // m
                             q_seq_len,   // n
                             k_seq_len,   // k
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(v_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(dropout_results.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta_zero),
                             static_cast<void*>(matmul2_results.data_ptr()), // C
                             CUDA_R_16F,
                             head_dim*attn_batches,
                             head_dim,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  outputs.copy_(output_biases);

  // Output Linear
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_T,
                             CUBLAS_OP_N,
                             embed_dim,
                             batches,
                             embed_dim,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(output_weights.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(matmul2_results.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(&beta_one),
                             static_cast<void*>(outputs.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             //CUBLAS_GEMM_ALGO1_TENSOR_OP));
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_DEFAULT_MATH));

  return {
           input_lin_results,
           attn_scores,
           dropout_results,
           dropout_mask,
           matmul2_results,
           outputs
         };
}

std::vector<torch::Tensor> bwd_cuda(
                               bool use_time_mask,
                               int                  heads,
                               torch::Tensor const& output_grads,
                               torch::Tensor const& matmul2_results,
                               torch::Tensor const& dropout_results,
                               torch::Tensor const& attn_scores,
//                                const half* pad_mask,
                               torch::Tensor const& input_lin_results,
                               torch::Tensor const& inputs,
                               torch::Tensor const& input_weights,
                               torch::Tensor const& output_weights,
                               torch::Tensor const& dropout_mask,
                               float                dropout_prob
                                   )
{
  const int   embed_dim      = inputs.size(2);
  const int   sequences      = inputs.size(1);
  const int   q_seq_len      = inputs.size(0);
  const int   k_seq_len      = q_seq_len;
  const int   batches        = sequences * q_seq_len;
  const int   head_dim       = embed_dim / heads;
  const int   output_lin_dim = 3 * embed_dim;
  const int   attn_batches   = heads * sequences;
  const int   lead_dim       = attn_batches * 3 * head_dim;
  const int   batch_stride   = 3 * head_dim;
//   const int   dropout_elems  = attn_batches * q_seq_len * k_seq_len;
  const float alpha          = 1.0;
  const float beta           = 0.0;
  const float scale          = 1.0 / sqrt(static_cast<float>(head_dim));
  const half halpha = __float2half_rn(alpha);
  const half hbeta = __float2half_rn(beta);
  const half hscale = __float2half_rn(scale);

  // TODO: Streams can be used in Backprop but I haven't added more than one
  // in my first attempt to create the code
  cublasHandle_t handle = at::cuda::getCurrentCUDABlasHandle();
  cudaStream_t   stream = at::cuda::getCurrentCUDAStream().stream();
  cublasSetStream(handle, stream);

  // Output Tensor Allocations
  torch::Tensor input_grads         = torch::empty_like(inputs);
  torch::Tensor input_weight_grads  = torch::empty_like(input_weights);
  torch::Tensor output_weight_grads = torch::empty_like(output_weights);
  // Intermediate Tensor Allocations
  at::Tensor output_lin_grads       = torch::empty_like(matmul2_results);
  at::Tensor matmul2_grads          = torch::empty_like(dropout_results);
  at::Tensor input_lin_output_grads = torch::empty_like(input_lin_results);

  auto q_lin_results_ptr = static_cast<half*>(input_lin_results.data_ptr());
  auto k_lin_results_ptr = static_cast<half*>(input_lin_results.data_ptr()) + head_dim;
  auto v_lin_results_ptr = static_cast<half*>(input_lin_results.data_ptr()) + 2*head_dim;

  auto q_lin_grads_ptr = static_cast<half*>(input_lin_output_grads.data_ptr());
  auto k_lin_grads_ptr = static_cast<half*>(input_lin_output_grads.data_ptr()) + head_dim;
  auto v_lin_grads_ptr = static_cast<half*>(input_lin_output_grads.data_ptr()) + 2*head_dim;

//  char a_layout_n{'n'};
//  char a_layout_t{'t'};
//  char b_layout_n{'n'};
//  char b_layout_t{'t'};

  TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_TENSOR_OP_MATH));

  // Output Linear Dgrad
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             embed_dim,
                             batches,
                             embed_dim,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(output_weights.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(output_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(output_lin_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));
  // Output Linear Wgrad
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_T,
                             embed_dim,
                             embed_dim,
                             batches,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(matmul2_results.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(output_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(output_weight_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  auto  output_bias_grads = output_grads.view({-1, embed_dim}).sum(0, false);
  // MatMul2 Dgrad1

  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_T,
                             CUBLAS_OP_N,
                             k_seq_len,    // m
                             q_seq_len,   // n
                             head_dim,   // k
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(v_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(output_lin_grads.data_ptr()),
                             CUDA_R_16F,
                             head_dim*attn_batches,
                             head_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(matmul2_grads.data_ptr()), // C
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Matmul2 Dgrad2
  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_T,
                             head_dim,    // m
                             k_seq_len,   // n
                             q_seq_len,   // k
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(output_lin_grads.data_ptr()),  // A:
                             CUDA_R_16F,
                             head_dim*attn_batches,  // lda
                             head_dim, // stride A
                             static_cast<const void*>(dropout_results.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(v_lin_grads_ptr), // C
                             CUDA_R_16F,
                             lead_dim,
                             batch_stride,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Apply Dropout Mask and Scale by Dropout Probability
  // Softmax Grad

  if ( dropout_prob > 0.0f) {
      dispatch_softmax_dropout_backward_recompute<half, half, float, false>(
                                     static_cast<half*>(matmul2_grads.data_ptr()),
                                     static_cast<const half*>(matmul2_grads.data_ptr()),
                                     reinterpret_cast<const half*>(attn_scores.data_ptr()), // need this to recompute softmax
                                     //reinterpret_cast<half const*>(pad_mask.data_ptr()),
                                     static_cast<uint8_t const*>(dropout_mask.data_ptr()),
                                     1.0/(1.0-dropout_prob),
                                     k_seq_len,
                                     k_seq_len,
                                     attn_batches*q_seq_len,
                                     stream);
  } else {
//       if dropout == 0 then we don't need to recompute (because dropout_results == softmax_results)
      dispatch_softmax_backward_norecompute<half, half, float, false>(
                                 static_cast<half*>(matmul2_grads.data_ptr()),
                                 static_cast<const half*>(matmul2_grads.data_ptr()),
                                 reinterpret_cast<const half*>(dropout_results.data_ptr()),
                                 k_seq_len,
                                 k_seq_len,
//                                  attn_batches*q_seq_len/sequences,
                                 attn_batches*q_seq_len,
                                 stream);
  }


  // Matmul1 Dgrad1

  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             head_dim,    // m
                             q_seq_len,   // n
                             k_seq_len,   // k
                             static_cast<const void*>(&scale),
                             static_cast<const void*>(k_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(matmul2_grads.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(q_lin_grads_ptr), // C
                             CUDA_R_16F,
                             lead_dim,
                             batch_stride,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Matmul1 Dgrad2

  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_T,
                             head_dim,    // m
                             k_seq_len,   // n
                             q_seq_len,   // k
                             static_cast<const void*>(&scale),
                             static_cast<const void*>(q_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(matmul2_grads.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(k_lin_grads_ptr), // C
                             CUDA_R_16F,
                             lead_dim,
                             batch_stride,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Input Linear Dgrad
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             embed_dim,
                             batches,
                             output_lin_dim,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(input_weights.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
			                 static_cast<const void*>(input_lin_output_grads.data_ptr()),
                             //static_cast<const void*>(q_lin_grads_ptr),
                             CUDA_R_16F,
                             output_lin_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(input_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             //CUBLAS_GEMM_ALGO10_TENSOR_OP));
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Input Linear Wgrad
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_T,
                             embed_dim,
                             output_lin_dim,
                             batches,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(inputs.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(q_lin_grads_ptr),
                             CUDA_R_16F,
                             output_lin_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(input_weight_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  auto  input_bias_grads = input_lin_output_grads.view({-1, output_lin_dim}).sum(0, false);
  TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_DEFAULT_MATH));

  return {
           input_grads,
           input_weight_grads,
           output_weight_grads,
           input_bias_grads,
           output_bias_grads
         };
}


torch::Tensor bwd_cuda_input_only(
                               bool use_time_mask,
                               int                  heads,
                               torch::Tensor const& output_grads,
                               torch::Tensor const& matmul2_results,
                               torch::Tensor const& dropout_results,
                               torch::Tensor const& attn_scores,
                               torch::Tensor const& input_lin_results,
                               torch::Tensor const& inputs,
                               torch::Tensor const& input_weights,
                               torch::Tensor const& output_weights,
                               torch::Tensor const& dropout_mask,
                               float                dropout_prob
                                   )
{
  const int   embed_dim      = inputs.size(2);
  const int   sequences      = inputs.size(1);
  const int   q_seq_len      = inputs.size(0);
  const int   k_seq_len      = q_seq_len;
  const int   batches        = sequences * q_seq_len;
  const int   head_dim       = embed_dim / heads;
  const int   output_lin_dim = 3 * embed_dim;
  const int   attn_batches   = heads * sequences;
  const int   lead_dim       = attn_batches * 3 * head_dim;
  const int   batch_stride   = 3 * head_dim;
//   const int   dropout_elems  = attn_batches * q_seq_len * k_seq_len;
  const float alpha          = 1.0;
  const float beta           = 0.0;
  const float scale          = 1.0 / sqrt(static_cast<float>(head_dim));
  const half halpha = __float2half_rn(alpha);
  const half hbeta = __float2half_rn(beta);
  const half hscale = __float2half_rn(scale);

  // TODO: Streams can be used in Backprop but I haven't added more than one
  // in my first attempt to create the code
  cublasHandle_t handle = at::cuda::getCurrentCUDABlasHandle();
  cudaStream_t   stream = at::cuda::getCurrentCUDAStream().stream();
  cublasSetStream(handle, stream);

  // Output Tensor Allocations
  torch::Tensor input_grads         = torch::empty_like(inputs);
//  torch::Tensor input_weight_grads  = torch::empty_like(input_weights);
//  torch::Tensor output_weight_grads = torch::empty_like(output_weights);
  // Intermediate Tensor Allocations
  at::Tensor output_lin_grads       = torch::empty_like(matmul2_results);
  at::Tensor matmul2_grads          = torch::empty_like(dropout_results);
  at::Tensor input_lin_output_grads = torch::empty_like(input_lin_results);

  auto q_lin_results_ptr = static_cast<half*>(input_lin_results.data_ptr());
  auto k_lin_results_ptr = static_cast<half*>(input_lin_results.data_ptr()) + head_dim;
  auto v_lin_results_ptr = static_cast<half*>(input_lin_results.data_ptr()) + 2*head_dim;

  auto q_lin_grads_ptr = static_cast<half*>(input_lin_output_grads.data_ptr());
  auto k_lin_grads_ptr = static_cast<half*>(input_lin_output_grads.data_ptr()) + head_dim;
  auto v_lin_grads_ptr = static_cast<half*>(input_lin_output_grads.data_ptr()) + 2*head_dim;

//  char a_layout_n{'n'};
//  char a_layout_t{'t'};
//  char b_layout_n{'n'};
//  char b_layout_t{'t'};

  TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_TENSOR_OP_MATH));

  // Output Linear Dgrad
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             embed_dim,
                             batches,
                             embed_dim,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(output_weights.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(output_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(output_lin_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));
  // Output Linear Wgrad
//   TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
//                              CUBLAS_OP_N,
//                              CUBLAS_OP_T,
//                              embed_dim,
//                              embed_dim,
//                              batches,
//                              static_cast<const void*>(&alpha),
//                              static_cast<const void*>(matmul2_results.data_ptr()),
//                              CUDA_R_16F,
//                              embed_dim,
//                              static_cast<const void*>(output_grads.data_ptr()),
//                              CUDA_R_16F,
//                              embed_dim,
//                              static_cast<const void*>(&beta),
//                              static_cast<void*>(output_weight_grads.data_ptr()),
//                              CUDA_R_16F,
//                              embed_dim,
//                              CUDA_R_32F,
//                              CUBLAS_GEMM_DEFAULT_TENSOR_OP));
//
//   auto  output_bias_grads = output_grads.view({-1, embed_dim}).sum(0, false);
  // MatMul2 Dgrad1

  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_T,
                             CUBLAS_OP_N,
                             k_seq_len,    // m
                             q_seq_len,   // n
                             head_dim,   // k
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(v_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(output_lin_grads.data_ptr()),
                             CUDA_R_16F,
                             head_dim*attn_batches,
                             head_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(matmul2_grads.data_ptr()), // C
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Matmul2 Dgrad2
  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_T,
                             head_dim,    // m
                             k_seq_len,   // n
                             q_seq_len,   // k
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(output_lin_grads.data_ptr()),  // A:
                             CUDA_R_16F,
                             head_dim*attn_batches,  // lda
                             head_dim, // stride A
                             static_cast<const void*>(dropout_results.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(v_lin_grads_ptr), // C
                             CUDA_R_16F,
                             lead_dim,
                             batch_stride,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Apply Dropout Mask and Scale by Dropout Probability
  // Softmax Grad

  if ( dropout_prob > 0.0f) {
      dispatch_softmax_dropout_backward_recompute<half, half, float, false>(
                                     static_cast<half*>(matmul2_grads.data_ptr()),
                                     static_cast<const half*>(matmul2_grads.data_ptr()),
                                     reinterpret_cast<const half*>(attn_scores.data_ptr()), // need this to recompute softmax
                                     //reinterpret_cast<half const*>(pad_mask.data_ptr()),
                                     static_cast<uint8_t const*>(dropout_mask.data_ptr()),
                                     1.0/(1.0-dropout_prob),
                                     k_seq_len,
                                     k_seq_len,
                                     attn_batches*q_seq_len,
                                     stream);
  } else {
//       if dropout == 0 then we don't need to recompute (because dropout_results == softmax_results)
      dispatch_softmax_backward_norecompute<half, half, float, false>(
                                 static_cast<half*>(matmul2_grads.data_ptr()),
                                 static_cast<const half*>(matmul2_grads.data_ptr()),
                                 reinterpret_cast<const half*>(dropout_results.data_ptr()),
                                 k_seq_len,
                                 k_seq_len,
//                                  attn_batches*q_seq_len/sequences,
                                 attn_batches*q_seq_len,
                                 stream);
  }


  // Matmul1 Dgrad1

  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             head_dim,    // m
                             q_seq_len,   // n
                             k_seq_len,   // k
                             static_cast<const void*>(&scale),
                             static_cast<const void*>(k_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(matmul2_grads.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(q_lin_grads_ptr), // C
                             CUDA_R_16F,
                             lead_dim,
                             batch_stride,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Matmul1 Dgrad2

  TORCH_CUDABLAS_CHECK(cublasGemmStridedBatchedEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_T,
                             head_dim,    // m
                             k_seq_len,   // n
                             q_seq_len,   // k
                             static_cast<const void*>(&scale),
                             static_cast<const void*>(q_lin_results_ptr),  // A:
                             CUDA_R_16F,
                             lead_dim,  // lda
                             batch_stride, // stride A
                             static_cast<const void*>(matmul2_grads.data_ptr()),
                             CUDA_R_16F,
                             k_seq_len,
                             k_seq_len*q_seq_len,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(k_lin_grads_ptr), // C
                             CUDA_R_16F,
                             lead_dim,
                             batch_stride,
                             attn_batches,
                             CUDA_R_32F,
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Input Linear Dgrad
  TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
                             CUBLAS_OP_N,
                             CUBLAS_OP_N,
                             embed_dim,
                             batches,
                             output_lin_dim,
                             static_cast<const void*>(&alpha),
                             static_cast<const void*>(input_weights.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
			                 static_cast<const void*>(input_lin_output_grads.data_ptr()),
                             //static_cast<const void*>(q_lin_grads_ptr),
                             CUDA_R_16F,
                             output_lin_dim,
                             static_cast<const void*>(&beta),
                             static_cast<void*>(input_grads.data_ptr()),
                             CUDA_R_16F,
                             embed_dim,
                             CUDA_R_32F,
                             //CUBLAS_GEMM_ALGO10_TENSOR_OP));
                             CUBLAS_GEMM_DEFAULT_TENSOR_OP));

  // Input Linear Wgrad
//   TORCH_CUDABLAS_CHECK(cublasGemmEx(handle,
//                              CUBLAS_OP_N,
//                              CUBLAS_OP_T,
//                              embed_dim,
//                              output_lin_dim,
//                              batches,
//                              static_cast<const void*>(&alpha),
//                              static_cast<const void*>(inputs.data_ptr()),
//                              CUDA_R_16F,
//                              embed_dim,
//                              static_cast<const void*>(q_lin_grads_ptr),
//                              CUDA_R_16F,
//                              output_lin_dim,
//                              static_cast<const void*>(&beta),
//                              static_cast<void*>(input_weight_grads.data_ptr()),
//                              CUDA_R_16F,
//                              embed_dim,
//                              CUDA_R_32F,
//                              CUBLAS_GEMM_DEFAULT_TENSOR_OP));
//
//   auto  input_bias_grads = input_lin_output_grads.view({-1, output_lin_dim}).sum(0, false);
  TORCH_CUDABLAS_CHECK(cublasSetMathMode(handle, CUBLAS_DEFAULT_MATH));

  return input_grads;
}

} // end namespace cublas_gemmex
} // end namespace self
} // end namespace multihead_attn