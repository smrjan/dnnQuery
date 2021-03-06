# Model modified by Hongyu Xiong: Deep neural parsing for database query
# specific model: (1) embedding_attention_seq2seq_pretrain
# (2) embedding_attention_seq2seq_pretrain2_tag
# (3) model_with_buckets_tag
# (4) functions are also outputting last encoder hidden state for PCA visual

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import numpy as np
import tensorflow as tf
# import module_cell as mc
import collections
import hashlib
import numbers
from tensorflow.python.ops import random_ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util

# We disable pylint because we need python3 compatibility.
from six.moves import xrange  # pylint: disable=redefined-builtin
from six.moves import zip  # pylint: disable=redefined-builtin

from tensorflow.contrib.rnn.python.ops import core_rnn
from tensorflow.contrib.rnn.python.ops import core_rnn_cell
from tensorflow.contrib.rnn.python.ops import core_rnn_cell_impl
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.util import nest


linear = core_rnn_cell_impl._linear  # pylint: disable=protected-access

#############################################################################
def _enumerated_map_structure(map_fn, *args, **kwargs):
  ix = [0]
  def enumerated_fn(*inner_args, **inner_kwargs):
    r = map_fn(ix[0], *inner_args, **inner_kwargs)
    ix[0] += 1
    return r
  return nest.map_structure(enumerated_fn, *args, **kwargs)

class DropoutWrapper(core_rnn_cell.RNNCell):
  """Operator adding dropout to inputs and outputs of the given cell."""

  def __init__(self, cell, input_keep_prob=1.0, output_keep_prob=1.0,
               state_keep_prob=1.0, variational_recurrent=False,
               input_size=None, dtype=None, seed=None):
    """Create a cell with added input, state, and/or output dropout.
    If `variational_recurrent` is set to `True` (**NOT** the default behavior),
    then the the same dropout mask is applied at every step, as described in:
    Y. Gal, Z Ghahramani.  "A Theoretically Grounded Application of Dropout in
    Recurrent Neural Networks".  https://arxiv.org/abs/1512.05287
    Otherwise a different dropout mask is applied at every time step.
    Args:
      cell: an RNNCell, a projection to output_size is added to it.
      input_keep_prob: unit Tensor or float between 0 and 1, input keep
        probability; if it is constant and 1, no input dropout will be added.
      output_keep_prob: unit Tensor or float between 0 and 1, output keep
        probability; if it is constant and 1, no output dropout will be added.
      state_keep_prob: unit Tensor or float between 0 and 1, output keep
        probability; if it is constant and 1, no output dropout will be added.
        State dropout is performed on the *output* states of the cell.
      variational_recurrent: Python bool.  If `True`, then the same
        dropout pattern is applied across all time steps per run call.
        If this parameter is set, `input_size` **must** be provided.
      input_size: (optional) (possibly nested tuple of) `TensorShape` objects
        containing the depth(s) of the input tensors expected to be passed in to
        the `DropoutWrapper`.  Required and used **iff**
         `variational_recurrent = True` and `input_keep_prob < 1`.
      dtype: (optional) The `dtype` of the input, state, and output tensors.
        Required and used **iff** `variational_recurrent = True`.
      seed: (optional) integer, the randomness seed.
    Raises:
      TypeError: if cell is not an RNNCell.
      ValueError: if any of the keep_probs are not between 0 and 1.
    """
    # if not _like_rnncell(cell):
    #   raise TypeError("The parameter cell is not a RNNCell.")
    with ops.name_scope("DropoutWrapperInit"):
      def tensor_and_const_value(v):
        tensor_value = ops.convert_to_tensor(v)
        const_value = tensor_util.constant_value(tensor_value)
        return (tensor_value, const_value)
      for prob, attr in [(input_keep_prob, "input_keep_prob"),
                         (state_keep_prob, "state_keep_prob"),
                         (output_keep_prob, "output_keep_prob")]:
        tensor_prob, const_prob = tensor_and_const_value(prob)
        if const_prob is not None:
          if const_prob < 0 or const_prob > 1:
            raise ValueError("Parameter %s must be between 0 and 1: %d"
                             % (attr, const_prob))
          setattr(self, "_%s" % attr, float(const_prob))
        else:
          setattr(self, "_%s" % attr, tensor_prob)

    # Set cell, variational_recurrent, seed before running the code below
    self._cell = cell
    self._variational_recurrent = variational_recurrent
    self._seed = seed

    self._recurrent_input_noise = None
    self._recurrent_state_noise = None
    self._recurrent_output_noise = None

    if variational_recurrent:
      if dtype is None:
        raise ValueError(
            "When variational_recurrent=True, dtype must be provided")

      def convert_to_batch_shape(s):
        # Prepend a 1 for the batch dimension; for recurrent
        # variational dropout we use the same dropout mask for all
        # batch elements.
        return array_ops.concat(
            ([1], tensor_shape.TensorShape(s).as_list()), 0)

      def batch_noise(s, inner_seed):
        shape = convert_to_batch_shape(s)
        return random_ops.random_uniform(shape, seed=inner_seed, dtype=dtype)

      if (not isinstance(self._input_keep_prob, numbers.Real) or
          self._input_keep_prob < 1.0):
        if input_size is None:
          raise ValueError(
              "When variational_recurrent=True and input_keep_prob < 1.0 or "
              "is unknown, input_size must be provided")
        self._recurrent_input_noise = _enumerated_map_structure(
            lambda i, s: batch_noise(s, inner_seed=self._gen_seed("input", i)),
            input_size)
      self._recurrent_state_noise = _enumerated_map_structure(
          lambda i, s: batch_noise(s, inner_seed=self._gen_seed("state", i)),
          cell.state_size)
      self._recurrent_output_noise = _enumerated_map_structure(
          lambda i, s: batch_noise(s, inner_seed=self._gen_seed("output", i)),
          cell.output_size)

  def _gen_seed(self, salt_prefix, index):
    if self._seed is None:
      return None
    salt = "%s_%d" % (salt_prefix, index)
    string = (str(self._seed) + salt).encode("utf-8")
    return int(hashlib.md5(string).hexdigest()[:8], 16) & 0x7FFFFFFF

  @property
  def state_size(self):
    return self._cell.state_size

  @property
  def output_size(self):
    return self._cell.output_size

  def zero_state(self, batch_size, dtype):
    with ops.name_scope(type(self).__name__ + "ZeroState", values=[batch_size]):
      return self._cell.zero_state(batch_size, dtype)

  def _variational_recurrent_dropout_value(
      self, index, value, noise, keep_prob):
    """Performs dropout given the pre-calculated noise tensor."""
    # uniform [keep_prob, 1.0 + keep_prob)
    random_tensor = keep_prob + noise

    # 0. if [keep_prob, 1.0) and 1. if [1.0, 1.0 + keep_prob)
    binary_tensor = math_ops.floor(random_tensor)
    ret = math_ops.div(value, keep_prob) * binary_tensor
    ret.set_shape(value.get_shape())
    return ret

  def _dropout(self, values, salt_prefix, recurrent_noise, keep_prob):
    """Decides whether to perform standard dropout or recurrent dropout."""
    if not self._variational_recurrent:
      def dropout(i, v):
        return nn_ops.dropout(
            v, keep_prob=keep_prob, seed=self._gen_seed(salt_prefix, i))
      return _enumerated_map_structure(dropout, values)
    else:
      def dropout(i, v, n):
        return self._variational_recurrent_dropout_value(i, v, n, keep_prob)
      return _enumerated_map_structure(dropout, values, recurrent_noise)

  def __call__(self, inputs, state, scope=None):
    """Run the cell with the declared dropouts."""
    def _should_dropout(p):
      return (not isinstance(p, float)) or p < 1

    if _should_dropout(self._input_keep_prob):
      inputs = self._dropout(inputs, "input",
                             self._recurrent_input_noise,
                             self._input_keep_prob)
    output, new_state = self._cell(inputs, state, scope)
    if _should_dropout(self._state_keep_prob):
      new_state = self._dropout(new_state, "state",
                                self._recurrent_state_noise,
                                self._state_keep_prob)
    if _should_dropout(self._output_keep_prob):
      output = self._dropout(output, "output",
                             self._recurrent_output_noise,
                             self._output_keep_prob)
    return output, new_state


#############################################################################

def _extract_argmax_and_embed(embedding,
                              output_projection=None,
                              update_embedding=True):
  """Get a loop_function that extracts the previous symbol and embeds it.
  Args:
    embedding: embedding tensor for symbols.
    output_projection: None or a pair (W, B). If provided, each fed previous
      output will first be multiplied by W and added B.
    update_embedding: Boolean; if False, the gradients will not propagate
      through the embeddings.
  Returns:
    A loop function.
  """

  def loop_function(prev, _):
    if output_projection is not None:
      prev = nn_ops.xw_plus_b(prev, output_projection[0], output_projection[1])
    prev_symbol = math_ops.argmax(prev, 1)
    # Note that gradients will not propagate through the second parameter of
    # embedding_lookup.
    emb_prev = embedding_ops.embedding_lookup(embedding, prev_symbol)
    if not update_embedding:
      emb_prev = array_ops.stop_gradient(emb_prev)
    return emb_prev

  return loop_function


def _extract_beamsearch_and_embed(beam_size,
                              embedding,
                              output_projection=None,
                              update_embedding=True):
  """Get a loop_function that extracts a list of previous sequence with beam_size and embeds them.
  Args:
    beam_size: number of branches in beam search
    embedding: embedding tensor for symbols.
    output_projection: None or a pair (W, B). If provided, each fed previous
      output will first be multiplied by W and added B.
    update_embedding: Boolean; if False, the gradients will not propagate
      through the embeddings.
  Returns:
    A loop function.
  """

  def loop_function(states, _):
    """Get a loop_function that extracts a list of previous sequence with beam_size and embeds them.
    Args:
      state: is a k-length list of states with (sequence, prob)
    Returns:
      promo: is a k-length list of promotion candidates with 
        (original, sequence, updated prob, current embed vector)
    """
    candidates = []
    # batch_size = len(states[0][0][0])
    for i in range(len(states)):
      sequence, prob, _= states[i]
      prev = sequence[-1]
      #prev = embedding_ops.embedding_lookup(embedding, prev)
      if output_projection is not None:
        prev = nn_ops.xw_plus_b(prev, output_projection[0], output_projection[1])
      # Finds values and indices of the k largest entries for the last dimension.
      new_probs, prev_symbols = nn_ops.top_k(prev, beam_size)
      for j in range(beam_size):
        emb_prev = embedding_ops.embedding_lookup(embedding, prev_symbols[:,j])
        if not update_embedding:
          # Note that gradients will not propagate through the second parameter of
          # embedding_lookup.
          emb_prev = array_ops.stop_gradient(emb_prev)
        candidates.append((sequence, prob * new_probs[:,j], emb_prev))
    
    # 1st transpose, convert a list of k^2 element into matrices
    prob_mat = tf.stack([y for (x, y, z) in candidates])
    emb_prev_mat = tf.stack([z for (x, y, z) in candidates])
    prob_mat = tf.transpose(prob_mat, perm=[1, 0], name='1st_transpose_value')
    emb_prev_mat = tf.transpose(emb_prev_mat, perm=[1, 0, 2], name='1st_transpose_vec')
    
    # Finds values and indices of the k largest entries for the last dimension.
    values, indices = nn_ops.top_k(prob_mat, beam_size)
    batch_size = tf.shape(prob_mat)[0]
    prob_mat2 = tf.zeros([batch_size, beam_size], tf.float32)
    emb_prev_mat2 = tf.zeros([batch_size, beam_size, tf.shape(emb_prev_mat)[2]], tf.float32)
    for k in range(batch_size.eval()):
      prob_mat2[k, :] = prob_mat[k, indices[k, :]]
      emb_prev_mat2[k, :, :] = emb_prev_mat[k, indices[k, :], :]
    
    # 2nd transpose
    indices = tf.transpose(indices, perm=[1, 0], name='transpose_indices')
    values = tf.transpose(values, perm=[1, 0], name='transpose_values')
    prob_mat2 = tf.transpose(prob_mat2, perm=[1, 0], name='2nd_transpose_value')
    emb_prev_mat2 = tf.transpose(emb_prev_mat2, perm=[1, 0, 2], name='2nd_transpose_value')
    
    # generate the promotion candidates
    promo = []
    T = len(states[0][0])
    output_size = tf.shape(states[0][0][0])[1]
    unit = tf.zeros([batch_size, output_size], tf.float32)
    for j in range(beam_size):
      # create a copy of a sequence
      new_sequence = [unit] * T
      # promo.append((indices[j,:], prob_mat2[j, :], emb_prev_mat2[j, :, :]))
      for k in range(batch_size.eval()):
        for t in range(T):
          corespo = candidates[indices[j,k]][0]
          new_sequence[t][k,:] = corespo[t][k,:]
      # new_sequence.append(indices[j,:])
      promo.append((new_sequence, prob_mat2[j, :], emb_prev_mat2[j, :, :]))
    
    return promo

  return loop_function


def rnn_decoder(decoder_inputs,
                initial_state,
                cell,
                loop_function=None,
                scope=None):
  """RNN decoder for the sequence-to-sequence model.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor with shape [batch_size x cell.state_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    loop_function: If not None, this function will be applied to the i-th output
      in order to generate the i+1-st input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    scope: VariableScope for the created subgraph; defaults to "rnn_decoder".
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing generated outputs.
      state: The state of each cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
        (Note that in some cases, like basic RNN cell or GRU cell, outputs and
         states can be the same. They are different for LSTM cells though.)
  """
  with variable_scope.variable_scope(scope or "rnn_decoder"):
    state = initial_state
    outputs = []
    prev = None
    for i, inp in enumerate(decoder_inputs):
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      output, state = cell(inp, state)
      outputs.append(output)
      if loop_function is not None:
        prev = output
  return outputs, state


def basic_rnn_seq2seq(encoder_inputs,
                      decoder_inputs,
                      cell,
                      dtype=dtypes.float32,
                      scope=None):
  """Basic RNN sequence-to-sequence model.
  This model first runs an RNN to encode encoder_inputs into a state vector,
  then runs decoder, initialized with the last encoder state, on decoder_inputs.
  Encoder and decoder use the same RNN cell type, but don't share parameters.
  Args:
    encoder_inputs: A list of 2D Tensors [batch_size x input_size].
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    dtype: The dtype of the initial state of the RNN cell (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "basic_rnn_seq2seq".
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell in the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(scope or "basic_rnn_seq2seq"):
    enc_cell = copy.deepcopy(cell)
    _, enc_state = core_rnn.static_rnn(enc_cell, encoder_inputs, dtype=dtype)
    return rnn_decoder(decoder_inputs, enc_state, cell)


def embedding_rnn_decoder(decoder_inputs,
                          initial_state,
                          cell,
                          num_symbols,
                          embedding_size,
                          output_projection=None,
                          feed_previous=False,
                          update_embedding_for_previous=True,
                          scope=None):
  """RNN decoder with embedding and a pure-decoding option.
  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    cell: core_rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has
      shape [num_symbols]; if provided and feed_previous=True, each fed
      previous output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    scope: VariableScope for the created subgraph; defaults to
      "embedding_rnn_decoder".
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors. The
        output is of shape [batch_size x cell.output_size] when
        output_projection is not None (and represents the dense representation
        of predicted tokens). It is of shape [batch_size x num_decoder_symbols]
        when output_projection is None.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  with variable_scope.variable_scope(scope or "embedding_rnn_decoder") as scope:
    if output_projection is not None:
      dtype = scope.dtype
      proj_weights = ops.convert_to_tensor(output_projection[0], dtype=dtype)
      proj_weights.get_shape().assert_is_compatible_with([None, num_symbols])
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      proj_biases.get_shape().assert_is_compatible_with([num_symbols])

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])
    loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None
    emb_inp = (embedding_ops.embedding_lookup(embedding, i)
               for i in decoder_inputs)
    return rnn_decoder(
        emb_inp, initial_state, cell, loop_function=loop_function)


def embedding_rnn_seq2seq(encoder_inputs,
                          decoder_inputs,
                          cell,
                          num_encoder_symbols,
                          num_decoder_symbols,
                          embedding_size,
                          output_projection=None,
                          feed_previous=False,
                          dtype=None,
                          scope=None):
  """Embedding RNN sequence-to-sequence model.
  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs RNN decoder, initialized with the last
  encoder state, on embedded decoder_inputs.
  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial state for both the encoder and encoder
      rnn cells (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_rnn_seq2seq"
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors. The
        output is of shape [batch_size x cell.output_size] when
        output_projection is not None (and represents the dense representation
        of predicted tokens). It is of shape [batch_size x num_decoder_symbols]
        when output_projection is None.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(scope or "embedding_rnn_seq2seq") as scope:
    if dtype is not None:
      scope.set_dtype(dtype)
    else:
      dtype = scope.dtype

    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    encoder_cell = core_rnn_cell.EmbeddingWrapper(
        encoder_cell,
        embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    _, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs, dtype=dtype)

    # Decoder.
    if output_projection is None:
      cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)

    if isinstance(feed_previous, bool):
      return embedding_rnn_decoder(
          decoder_inputs,
          encoder_state,
          cell,
          num_decoder_symbols,
          embedding_size,
          output_projection=output_projection,
          feed_previous=feed_previous)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_rnn_decoder(
            decoder_inputs,
            encoder_state,
            cell,
            num_decoder_symbols,
            embedding_size,
            output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state


def attention_decoder(decoder_inputs,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False):
  """RNN decoder with attention for the sequence-to-sequence model.
  In this context "attention" means that, during decoding, the RNN can look up
  information in the additional tensor attention_states, and it does this by
  focusing on a few entries from the tensor. This model has proven to yield
  especially good results in a number of sequence-to-sequence tasks. This
  implementation is based on http://arxiv.org/abs/1412.7449 (see below for
  details). It is recommended for complex sequence-to-sequence tasks.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    output_size: Size of the output vectors; if None, we use cell.output_size.
    num_heads: Number of attention heads that read from attention_states.
    loop_function: If not None, this function will be applied to i-th output
      in order to generate i+1-th input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    dtype: The dtype to use for the RNN initial state (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors of
        shape [batch_size x output_size]. These represent the generated outputs.
        Output i is computed from input i (which is either the i-th element
        of decoder_inputs or loop_function(output {i-1}, i)) as follows.
        First, we run the cell on a combination of the input and previous
        attention masks:
          cell_output, new_state = cell(linear(input, prev_attn), prev_state).
        Then, we calculate new attention masks:
          new_attn = softmax(V^T * tanh(W * attention_states + U * new_state))
        and then we calculate the output:
          output = linear(cell_output, new_attn).
      state: The state of each decoder cell the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: when num_heads is not positive, there are no inputs, shapes
      of attention_states are not set, or input size cannot be inferred
      from the input.
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prev = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)
    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)
      x = linear([inp] + attns, input_size, True)
      # Run the RNN.
      cell_output, state = cell(x, state)
      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(
            variable_scope.get_variable_scope(), reuse=True):
          attns = attention(state)
      else:
        attns = attention(state)

      with variable_scope.variable_scope("AttnOutputProjection"):
        output = linear([cell_output] + attns, output_size, True)
      if loop_function is not None:
        prev = output
      outputs.append(output)

  return outputs, state

def attention_decoder_confusion(decoder_inputs,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False):
  """RNN decoder with attention for the sequence-to-sequence model.
  Returns:
    A tuple of the form (outputs, state, confusion_matrix), where:
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      # 0531 newly added
      acon = None
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          # 0531 newly modified
          acon = nn_ops.softmax(s)  # shape[batch_size, attn_length]
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(acon, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds, acon

    outputs = []
    confusion_matrix = []   # 0531 newly added
    prev = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)      
    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)
      x = linear([inp] + attns, input_size, True)
      # Run the RNN.
      cell_output, state = cell(x, state)
      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(
            variable_scope.get_variable_scope(), reuse=True):
          # 0531 newly modified/added
          attns, acon_step = attention(state)
          #acon_step = np.reshape(acon_step, [acon_step.shape[0], acon_step.shape[1], 1])
          confusion_matrix.append(acon_step)
      else:
        # 0531 newly modified/added
        attns, acon_step = attention(state)
        #acon_step = np.reshape(acon_step, [acon_step.shape[0], acon_step.shape[1], 1])
        confusion_matrix.append(acon_step)
      
      with variable_scope.variable_scope("AttnOutputProjection"):
        output = linear([cell_output] + attns, output_size, True)
      if loop_function is not None:
        prev = output
      outputs.append(output)
    # 0531 newly added
    # confusion_matrix = np.concatenate(confusion_matrix, axis=2)
    confusion_matrix = tf.stack(confusion_matrix, axis=2)

  return outputs, state, confusion_matrix


def attention_decoder_beamsearch(decoder_inputs,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False):
  """RNN decoder with attention for the sequence-to-sequence model.
      Based on Beam Search (NOt complete yet!)
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder_beamsearch", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prevs = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)
    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prevs is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          promo = loop_function(prevs, i)
      else: 
        prob = tf.ones([batch_size,], tf.float32)
        promo = [(outputs, prob, inp)]
      # Using beamsearch loop function, keep top k candidates
      new_prevs = []
      for j in range(len(promo)):
        sequence, prob, inp = promo[j]
        # Merge input and previous attentions into one vector of the right size.
        input_size = inp.get_shape().with_rank(2)[1]
        if input_size.value is None:
          raise ValueError("Could not infer input size from input: %s" % inp.name)
        x = linear([inp] + attns, input_size, True)
        # Run the RNN.
        cell_output, state = cell(x, state)
        # Run the attention mechanism.
        if i == 0 and initial_state_attention:
          with variable_scope.variable_scope(
              variable_scope.get_variable_scope(), reuse=True):
            attns = attention(state)
        else:
          attns = attention(state)

        with variable_scope.variable_scope("AttnOutputProjection"):
          output = linear([cell_output] + attns, output_size, True)
        #if loop_function is not None:
        sequence.append(output)
        new_prevs.append((sequence, prob, state))
      prevs = new_prevs
      outputs = prevs[0][0]
      state = prevs[0][2]

  return outputs, state


def embedding_attention_decoder(decoder_inputs,
                                initial_state,
                                attention_states,
                                cell,
                                num_symbols,
                                embedding_size,
                                num_heads=1,
                                output_size=None,
                                output_projection=None,
                                feed_previous=False,
                                update_embedding_for_previous=True,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """RNN decoder with embedding and attention and a pure-decoding option.
  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_size: Size of the output vectors; if None, use output_size.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has shape
      [num_symbols]; if provided and feed_previous=True, each fed previous
      output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    dtype: The dtype to use for the RNN initial states (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  if output_size is None:
    output_size = cell.output_size
  if output_projection is not None:
    proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
    proj_biases.get_shape().assert_is_compatible_with([num_symbols])

  with variable_scope.variable_scope(
      scope or "embedding_attention_decoder", dtype=dtype) as scope:

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])
    loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None
    emb_inp = [
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs
    ]
    return attention_decoder(
        emb_inp,
        initial_state,
        attention_states,
        cell,
        output_size=output_size,
        num_heads=num_heads,
        loop_function=loop_function,
        initial_state_attention=initial_state_attention)


def embedding_attention_seq2seq(encoder_inputs,
                                decoder_inputs,
                                cell,
                                num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. It keeps the outputs of this
  RNN at every step to use for attention later. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs attention decoder, initialized with the last
  encoder state, on embedded decoder_inputs and attending to encoder outputs.
  Warning: when output_projection is None, the size of the attention vectors
  and variables will be made proportional to num_decoder_symbols, can be large.
  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial RNN state (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_seq2seq".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    encoder_cell = core_rnn_cell.EmbeddingWrapper(
        encoder_cell,
        embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    if output_projection is None:
      cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
      output_size = num_decoder_symbols

    if isinstance(feed_previous, bool):
      return embedding_attention_decoder(
          decoder_inputs,
          encoder_state,
          attention_states,
          cell,
          num_decoder_symbols,
          embedding_size,
          num_heads=num_heads,
          output_size=output_size,
          output_projection=output_projection,
          feed_previous=feed_previous,
          initial_state_attention=initial_state_attention)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_attention_decoder(
            decoder_inputs,
            encoder_state,
            attention_states,
            cell,
            num_decoder_symbols,
            embedding_size,
            num_heads=num_heads,
            output_size=output_size,
            output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=True,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state

def embedding_attention_seq2seq_pretrain(encoder_inputs,
                                decoder_inputs,
                                cell,
                                embedding_matrix,
                                #num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size = 50,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. It keeps the outputs of this
  RNN at every step to use for attention later. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs attention decoder, initialized with the last
  encoder state, on embedded decoder_inputs and attending to encoder outputs.
  Warning: when output_projection is None, the size of the attention vectors
  and variables will be made proportional to num_decoder_symbols, can be large.
  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial RNN state (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_seq2seq".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq_pretrain", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    # encoder_cell = core_rnn_cell.EmbeddingWrapper(
    #     encoder_cell,
    #     embedding_classes=num_encoder_symbols,
    #     embedding_size=embedding_size)
    
    ### INPUT here should be embedded after look up
    ### pretrained_embeddings = tf.Variable(embedding_matrix, trainable = False)
    ### encoder_cell = encoder_cell()
    ### embeddings = tf.Variable(self.pretrained_embeddings)                                                     
    ### encoder_inputs = tf.nn.embedding_lookup(embedding_matrix, encoder_inputs)
    embedding_matrix = tf.Variable(embedding_matrix, trainable = False)
    encoder_inputs_embed = []
    for i in range(len(encoder_inputs)):
      encoder_inputs_embed.append(embedding_ops.embedding_lookup(embedding_matrix, encoder_inputs[i]))
    ### encoder_inputs = tf.reshape(encoder_inputs, [])   
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs_embed, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    if output_projection is None:
      cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
      output_size = num_decoder_symbols

    if isinstance(feed_previous, bool):
      outputs, state = embedding_attention_decoder(
          decoder_inputs,
          encoder_state,
          attention_states,
          cell,
          num_decoder_symbols,
          embedding_size,
          num_heads=num_heads,
          output_size=output_size,
          output_projection=output_projection,
          feed_previous=feed_previous,
          update_embedding_for_previous=True,
          initial_state_attention=initial_state_attention)
      return outputs, state, encoder_state ### the last hidden state of encoder

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_attention_decoder(
            decoder_inputs,
            encoder_state,
            attention_states,
            cell,
            num_decoder_symbols,
            embedding_size,
            num_heads=num_heads,
            output_size=output_size,
            output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=True,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state, encoder_state ### the last hidden state of encoder

def embedding_attention_seq2seq_pretrain3_tag(encoder_inputs,
                                tag_inputs,
                                decoder_inputs,
                                cell,
                                embedding_matrix_from,
                                embedding_matrix_to,
                                #num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size = 100,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq_pretrain3_tag", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    embedding_matrix_from = tf.Variable(embedding_matrix_from, trainable = False)
    embedding_matrix_to = tf.Variable(embedding_matrix_to, trainable = True)  # decoder vocab vectors could be trained
    
    encoder_inputs_embed = []
    #tag_inputs_embed = []
    decoder_inputs_embed = []
    for i in range(len(encoder_inputs)):
      encoder_step = embedding_ops.embedding_lookup(embedding_matrix_from, encoder_inputs[i])
      tag_step = embedding_ops.embedding_lookup(embedding_matrix_to, tag_inputs[i])
      ##### Concate encoder input with corresponding tags
      encoder_inputs_embed.append(tf.concat([encoder_step, tag_step], 1))
    for i in range(len(decoder_inputs)):
      decoder_inputs_embed.append(embedding_ops.embedding_lookup(embedding_matrix_to, decoder_inputs[i]))
    ### encoder_inputs = tf.reshape(encoder_inputs, [])   
    
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs_embed, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    # if output_projection is None:
    #   cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
    #   output_size = num_decoder_symbols

    if output_size is None:
      output_size = cell.output_size
    if output_projection is not None:
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      #num_symbols = embedding_matrix_to.get_shape()[0]
      proj_biases.get_shape().assert_is_compatible_with([num_decoder_symbols])

    loop_function = _extract_beamsearch_and_embed(20,
        embedding_matrix_to, output_projection, True) if feed_previous else None
  
    if isinstance(feed_previous, bool):
      outputs, state = attention_decoder_beamsearch(
          decoder_inputs_embed,
          encoder_state,
          attention_states,
          cell,
          output_size=output_size,
          num_heads=num_heads,
          loop_function=loop_function,
          initial_state_attention=initial_state_attention)

      return outputs, state, encoder_state ### the last hidden state of encoder

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
        variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = attention_decoder_beamsearch(
            decoder_inputs_embed,
            encoder_state,
            attention_states,
            cell,
            output_size=output_size,
            num_heads=num_heads,
            loop_function=loop_function,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state, encoder_state ### the last hidden state of encoder


def embedding_attention_seq2seq_pretrain_tag(encoder_inputs,
                                tag_inputs,
                                decoder_inputs,
                                cell,
                                embedding_matrix_from,
                                embedding_matrix_to,
                                #num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size = 100,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq_pretrain_tag", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    embedding_matrix_from = tf.Variable(embedding_matrix_from, trainable = False)
    embedding_matrix_to = tf.Variable(embedding_matrix_to, trainable = True)  # decoder vocab vectors could be trained
    
    encoder_inputs_embed = []
    #tag_inputs_embed = []
    decoder_inputs_embed = []
    for i in range(len(encoder_inputs)):
      encoder_step = embedding_ops.embedding_lookup(embedding_matrix_from, encoder_inputs[i])
      # tag_step = embedding_ops.embedding_lookup(embedding_matrix_from, tag_inputs[i])
      tag_step = embedding_ops.embedding_lookup(embedding_matrix_to, tag_inputs[i])
      ##### Concate encoder input with corresponding tags
      encoder_inputs_embed.append(tf.concat([encoder_step, tag_step], 1))
    for i in range(len(decoder_inputs)):
      decoder_inputs_embed.append(embedding_ops.embedding_lookup(embedding_matrix_to, decoder_inputs[i]))
    ### encoder_inputs = tf.reshape(encoder_inputs, [])   
    
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs_embed, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    # if output_projection is None:
    #   cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
    #   output_size = num_decoder_symbols

    if output_size is None:
      output_size = cell.output_size
    if output_projection is not None:
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      #num_symbols = embedding_matrix_to.get_shape()[0]
      proj_biases.get_shape().assert_is_compatible_with([num_decoder_symbols])

    loop_function = _extract_argmax_and_embed(
        embedding_matrix_to, output_projection, True) if feed_previous else None
  
    if isinstance(feed_previous, bool):
      outputs, state = attention_decoder(
          decoder_inputs_embed,
          encoder_state,
          attention_states,
          cell,
          output_size=output_size,
          num_heads=num_heads,
          loop_function=loop_function,
          initial_state_attention=initial_state_attention)

      return outputs, state, encoder_state ### the last hidden state of encoder

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
        variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = attention_decoder(
            decoder_inputs_embed,
            encoder_state,
            attention_states,
            cell,
            output_size=output_size,
            num_heads=num_heads,
            loop_function=loop_function,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state, encoder_state ### the last hidden state of encoder


def embedding_attention_seq2seq_pretrain2_tag_nocon(encoder_inputs,
                                tag_inputs,
                                decoder_inputs,
                                cell,
                                embedding_matrix_from,
                                embedding_matrix_to,
                                #num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size = 300,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  Returns:
    A tuple of the form (outputs, state, last_hidden), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq_pretrain2_tag_nocon", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    embedding_matrix_from = tf.Variable(embedding_matrix_from, trainable = True)
    embedding_matrix_to_pre = tf.Variable(embedding_matrix_to, trainable = True)  # decoder vocab vectors could be trained
    # tag part of the variable
    tag_matrix = np.random.normal(0, 1, (12, 100))
    tag_matrix = tf.Variable(tag_matrix, trainable = True, dtype=dtype)
    embedding_matrix_tag = tf.stack([tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[4]], 0)])
    # recombine output embedding
    embedding_matrix_to = tf.concat([embedding_matrix_to_pre[0:5], embedding_matrix_tag, embedding_matrix_to_pre[55:]], 0)
    
    encoder_inputs_embed = []
    #tag_inputs_embed = []
    decoder_inputs_embed = []
    for i in range(len(encoder_inputs)):
      encoder_step = embedding_ops.embedding_lookup(embedding_matrix_from, encoder_inputs[i])
      # tag_step = embedding_ops.embedding_lookup(embedding_matrix_from, tag_inputs[i])
      tag_step = embedding_ops.embedding_lookup(embedding_matrix_to, tag_inputs[i])
      ##### Concate encoder input with corresponding tags
      encoder_inputs_embed.append(tf.concat([encoder_step, tag_step], 1))
    for i in range(len(decoder_inputs)):
      decoder_inputs_embed.append(embedding_ops.embedding_lookup(embedding_matrix_to, decoder_inputs[i]))
    ### encoder_inputs = tf.reshape(encoder_inputs, [])   
    
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs_embed, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    # if output_projection is None:
    #   cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
    #   output_size = num_decoder_symbols

    if output_size is None:
      output_size = cell.output_size
    if output_projection is not None:
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      #num_symbols = embedding_matrix_to.get_shape()[0]
      proj_biases.get_shape().assert_is_compatible_with([num_decoder_symbols])

    loop_function = _extract_argmax_and_embed(
        embedding_matrix_to, output_projection, True) if feed_previous else None
  
    if isinstance(feed_previous, bool):
      outputs, state = attention_decoder(
          decoder_inputs_embed,
          encoder_state,
          attention_states,
          cell,
          output_size=output_size,
          num_heads=num_heads,
          loop_function=loop_function,
          initial_state_attention=initial_state_attention)

      return outputs, state, encoder_state ### the last hidden state of encoder

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
        variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = attention_decoder(
            decoder_inputs_embed,
            encoder_state,
            attention_states,
            cell,
            output_size=output_size,
            num_heads=num_heads,
            loop_function=loop_function,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state, encoder_state ### the last hidden state of encoder


def embedding_attention_seq2seq_pretrain2_tag(encoder_inputs,
                                tag_inputs,
                                decoder_inputs,
                                cell,
                                embedding_matrix_from,
                                embedding_matrix_to,
                                #num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size = 300,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  Returns:
    A tuple of the form (outputs, state, confusion_matrix), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq_pretrain2_tag", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    # dropput
    if not feed_previous:
      encoder_cell = DropoutWrapper(encoder_cell, #core_rnn_cell_impl.
                                           input_keep_prob=0.7, 
                                           output_keep_prob=0.5,
                                           #state_keep_prob=0.95,
                                           variational_recurrent=True,
                                           input_size=embedding_size*2,
                                           dtype=dtype
                                           )
    embedding_matrix_from = tf.Variable(embedding_matrix_from, trainable = True, dtype=dtype)
    embedding_matrix_to_pre = tf.Variable(embedding_matrix_to, trainable = False, dtype=dtype)  # decoder vocab vectors could be trained
    # tag part of the variable
    tag_matrix = np.random.normal(0, 1, (12, 100))
    tag_matrix = tf.Variable(tag_matrix, trainable = True, dtype=dtype)
    embedding_matrix_tag = tf.stack([tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[7], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[7], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[8], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[8], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[9], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[9], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[10], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[10], tag_matrix[4]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[11], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[11], tag_matrix[4]], 0)])
    # recombine output embedding
    embedding_matrix_to = tf.concat([embedding_matrix_to_pre[0:5], embedding_matrix_tag, embedding_matrix_to_pre[55:]], 0)
    
    encoder_inputs_embed = []
    #tag_inputs_embed = []
    decoder_inputs_embed = []
    for i in range(len(encoder_inputs)):
      encoder_step = embedding_ops.embedding_lookup(embedding_matrix_from, encoder_inputs[i])
      # tag_step = embedding_ops.embedding_lookup(embedding_matrix_from, tag_inputs[i])
      tag_step = embedding_ops.embedding_lookup(embedding_matrix_to, tag_inputs[i])
      ##### Concate encoder input with corresponding tags
      encoder_inputs_embed.append(tf.concat([encoder_step, tag_step], 1))
    for i in range(len(decoder_inputs)):
      decoder_inputs_embed.append(embedding_ops.embedding_lookup(embedding_matrix_to, decoder_inputs[i]))
    ### encoder_inputs = tf.reshape(encoder_inputs, [])   
    
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs_embed, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    # if output_projection is None:
    #   cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
    #   output_size = num_decoder_symbols

    if output_size is None:
      output_size = cell.output_size
    if output_projection is not None:
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      #num_symbols = embedding_matrix_to.get_shape()[0]
      proj_biases.get_shape().assert_is_compatible_with([num_decoder_symbols])

    loop_function = _extract_argmax_and_embed(
        embedding_matrix_to, output_projection, True) if feed_previous else None
    
    decoder_cell = copy.deepcopy(cell)
    # dropput
    if not feed_previous:
      decoder_cell = DropoutWrapper(decoder_cell, #core_rnn_cell_impl.
                                           input_keep_prob=0.7, 
                                           output_keep_prob=0.5,
                                           #state_keep_prob=0.95,
                                           variational_recurrent=True,
                                           input_size=embedding_size,
                                           dtype=dtype
                                           )
    if isinstance(feed_previous, bool):
      outputs, state, confusion_matrix = attention_decoder_confusion(
          decoder_inputs_embed,
          encoder_state,
          attention_states,
          decoder_cell,
          output_size=output_size,
          num_heads=num_heads,
          loop_function=loop_function,
          initial_state_attention=initial_state_attention)

      return outputs, state, confusion_matrix ##

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
        variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state, confusion_matrix = attention_decoder_confusion(
            decoder_inputs_embed,
            encoder_state,
            attention_states,
            decoder_cell,
            output_size=output_size,
            num_heads=num_heads,
            loop_function=loop_function,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state, confusion_matrix ##


def embedding_attention_seq2seq_pretrain2_X(batch_size, 
                                encoder_inputs,
                                tag_inputs,
                                decoder_inputs,
                                cell,
                                embedding_matrix_from,
                                embedding_matrix_to,
                                #num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size = 100,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  """Embedding sequence-to-sequence model with attention.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq_pretrain2_X", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    embedding_matrix_from = tf.Variable(embedding_matrix_from, trainable = False)
    embedding_matrix_to_pre = tf.Variable(embedding_matrix_to, trainable = True)  # decoder vocab vectors could be trained
    # tag part of the variable
    tag_matrix = np.zeros((7, 50), dtype = 'float32')
    for i in range(5):
      tag_matrix[i] = embedding_matrix_to[5+i, 50:]
    tag_matrix[5] = embedding_matrix_to[5, 0:50]
    tag_matrix[6] = embedding_matrix_to[10, 0:50]
    tag_matrix = tf.Variable(tag_matrix, trainable = True)
    embedding_matrix_tag = tf.stack([tf.concat([tag_matrix[5], tag_matrix[0]], 0), tf.concat([tag_matrix[5], tag_matrix[1]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[2]], 0), tf.concat([tag_matrix[5], tag_matrix[3]], 0),
                                    tf.concat([tag_matrix[5], tag_matrix[4]], 0), tf.concat([tag_matrix[6], tag_matrix[0]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[1]], 0), tf.concat([tag_matrix[6], tag_matrix[2]], 0),
                                    tf.concat([tag_matrix[6], tag_matrix[3]], 0), tf.concat([tag_matrix[6], tag_matrix[4]], 0)])
    # recombine output embedding
    embedding_matrix_to = tf.concat([embedding_matrix_to_pre[0:5], embedding_matrix_tag, embedding_matrix_to_pre[15:]], 0)
    
    encoder_inputs_embed = []
    #tag_inputs_embed = []
    decoder_inputs_embed = []
    for i in range(len(encoder_inputs)):
      encoder_step = embedding_ops.embedding_lookup(embedding_matrix_from, encoder_inputs[i])
      # tag_step = embedding_ops.embedding_lookup(embedding_matrix_from, tag_inputs[i])
      tag_step = embedding_ops.embedding_lookup(embedding_matrix_to, tag_inputs[i])
      ##### Replace encoder input with corresponding tags
      encoder_step_list = tf.unstack(encoder_step, batch_size, axis=0)
      tag_step_list = tf.unstack(tag_step, batch_size, axis=0)
      embed_list = []
      for j in range(batch_size):
        if tag_step_list[j] == embedding_matrix_to[4]:
          embed_list.append(encoder_step_list[j])
        else:
          embed_list.append(tag_step_list[j])
      encoder_inputs_embed.append(tf.stack(embed_list, 0))
    for i in range(len(decoder_inputs)):
      decoder_inputs_embed.append(embedding_ops.embedding_lookup(embedding_matrix_to, decoder_inputs[i]))
    ### encoder_inputs = tf.reshape(encoder_inputs, [])   
    
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs_embed, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    # if output_projection is None:
    #   cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
    #   output_size = num_decoder_symbols

    if output_size is None:
      output_size = cell.output_size
    if output_projection is not None:
      proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
      #num_symbols = embedding_matrix_to.get_shape()[0]
      proj_biases.get_shape().assert_is_compatible_with([num_decoder_symbols])

    loop_function = _extract_argmax_and_embed(
        embedding_matrix_to, output_projection, True) if feed_previous else None
  
    if isinstance(feed_previous, bool):
      outputs, state = attention_decoder(
          decoder_inputs_embed,
          encoder_state,
          attention_states,
          cell,
          output_size=output_size,
          num_heads=num_heads,
          loop_function=loop_function,
          initial_state_attention=initial_state_attention)

      return outputs, state, encoder_state ### the last hidden state of encoder

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
        variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = attention_decoder(
            decoder_inputs_embed,
            encoder_state,
            attention_states,
            cell,
            output_size=output_size,
            num_heads=num_heads,
            loop_function=loop_function,
            initial_state_attention=initial_state_attention)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state, encoder_state ### the last hidden state of encoder


def sequence_loss_by_example(logits,
                             targets,
                             weights,
                             average_across_timesteps=True,
                             softmax_loss_function=None,
                             name=None):
  """Weighted cross-entropy loss for a sequence of logits (per example).
  Args:
    logits: List of 2D Tensors of shape [batch_size x num_decoder_symbols].
    targets: List of 1D batch-sized int32 Tensors of the same length as logits.
    weights: List of 1D batch-sized float-Tensors of the same length as logits.
    average_across_timesteps: If set, divide the returned cost by the total
      label weight.
    softmax_loss_function: Function (labels-batch, inputs-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    name: Optional name for this operation, default: "sequence_loss_by_example".
  Returns:
    1D batch-sized float Tensor: The log-perplexity for each sequence.
  Raises:
    ValueError: If len(logits) is different from len(targets) or len(weights).
  """
  if len(targets) != len(logits) or len(weights) != len(logits):
    raise ValueError("Lengths of logits, weights, and targets must be the same "
                     "%d, %d, %d." % (len(logits), len(weights), len(targets)))
  with ops.name_scope(name, "sequence_loss_by_example",
                      logits + targets + weights):
    log_perp_list = []
    for logit, target, weight in zip(logits, targets, weights):
      if softmax_loss_function is None:
        # TODO(irving,ebrevdo): This reshape is needed because
        # sequence_loss_by_example is called with scalars sometimes, which
        # violates our general scalar strictness policy.
        target = array_ops.reshape(target, [-1])
        crossent = nn_ops.sparse_softmax_cross_entropy_with_logits(
            labels=target, logits=logit)
      else:
        crossent = softmax_loss_function(target, logit)
      log_perp_list.append(crossent * weight)
    log_perps = math_ops.add_n(log_perp_list)
    if average_across_timesteps:
      total_size = math_ops.add_n(weights)
      total_size += 1e-12  # Just to avoid division by 0 for all-0 weights.
      log_perps /= total_size
  return log_perps


def sequence_loss(logits,
                  targets,
                  weights,
                  average_across_timesteps=True,
                  average_across_batch=True,
                  softmax_loss_function=None,
                  name=None):
  """Weighted cross-entropy loss for a sequence of logits, batch-collapsed.
  Args:
    logits: List of 2D Tensors of shape [batch_size x num_decoder_symbols].
    targets: List of 1D batch-sized int32 Tensors of the same length as logits.
    weights: List of 1D batch-sized float-Tensors of the same length as logits.
    average_across_timesteps: If set, divide the returned cost by the total
      label weight.
    average_across_batch: If set, divide the returned cost by the batch size.
    softmax_loss_function: Function (inputs-batch, labels-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    name: Optional name for this operation, defaults to "sequence_loss".
  Returns:
    A scalar float Tensor: The average log-perplexity per symbol (weighted).
  Raises:
    ValueError: If len(logits) is different from len(targets) or len(weights).
  """
  with ops.name_scope(name, "sequence_loss", logits + targets + weights):
    cost = math_ops.reduce_sum(
        sequence_loss_by_example(
            logits,
            targets,
            weights,
            average_across_timesteps=average_across_timesteps,
            softmax_loss_function=softmax_loss_function))
    if average_across_batch:
      batch_size = array_ops.shape(targets[0])[0]
      return cost / math_ops.cast(batch_size, cost.dtype)
    else:
      return cost


def model_with_buckets_tag(encoder_inputs,
                       tag_inputs,
                       decoder_inputs,
                       targets,
                       weights,
                       buckets,
                       seq2seq,
                       softmax_loss_function=None,
                       per_example_loss=False,
                       name=None):
  """Create a sequence-to-sequence model with support for bucketing.
  The seq2seq argument is a function that defines a sequence-to-sequence model,
  e.g., seq2seq = lambda x, y: basic_rnn_seq2seq(
      x, y, core_rnn_cell.GRUCell(24))
  Args:
    encoder_inputs: A list of Tensors to feed the encoder; first seq2seq input.
    decoder_inputs: A list of Tensors to feed the decoder; second seq2seq input.
    targets: A list of 1D batch-sized int32 Tensors (desired output sequence).
    weights: List of 1D batch-sized float-Tensors to weight the targets.
    buckets: A list of pairs of (input size, output size) for each bucket.
    seq2seq: A sequence-to-sequence model function; it takes 2 input that
      agree with encoder_inputs and decoder_inputs, and returns a pair
      consisting of outputs and states (as, e.g., basic_rnn_seq2seq).
    softmax_loss_function: Function (inputs-batch, labels-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    per_example_loss: Boolean. If set, the returned loss will be a batch-sized
      tensor of losses for each sequence in the batch. If unset, it will be
      a scalar with the averaged loss from all examples.
    name: Optional name for this operation, defaults to "model_with_buckets".
  Returns:
    A tuple of the form (outputs, losses), where:
      outputs: The outputs for each bucket. Its j'th element consists of a list
        of 2D Tensors. The shape of output tensors can be either
        [batch_size x output_size] or [batch_size x num_decoder_symbols]
        depending on the seq2seq model used.
      losses: List of scalar Tensors, representing losses for each bucket, or,
        if per_example_loss is set, a list of 1D batch-sized float Tensors.
  Raises:
    ValueError: If length of encoder_inputsut, targets, or weights is smaller
      than the largest (last) bucket.
  """
  if len(encoder_inputs) < buckets[-1][0]:
    raise ValueError("Length of encoder_inputs (%d) must be at least that of la"
                     "st bucket (%d)." % (len(encoder_inputs), buckets[-1][0]))
  if len(tag_inputs) < buckets[-1][0]:
    raise ValueError("Length of tag_inputs (%d) must be at least that of la"
                     "st bucket (%d)." % (len(tag_inputs), buckets[-1][0]))
  if len(targets) < buckets[-1][1]:
    raise ValueError("Length of targets (%d) must be at least that of last"
                     "bucket (%d)." % (len(targets), buckets[-1][1]))
  if len(weights) < buckets[-1][1]:
    raise ValueError("Length of weights (%d) must be at least that of last"
                     "bucket (%d)." % (len(weights), buckets[-1][1]))

  all_inputs = encoder_inputs + tag_inputs + decoder_inputs + targets + weights
  losses = []
  outputs = []
  ### output the last hidden state of encoder
  lasthiddens = []
  with ops.name_scope(name, "model_with_buckets_tag", all_inputs):
    for j, bucket in enumerate(buckets):
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=True if j > 0 else None):
        bucket_outputs, _ , bucket_lasthiddens= seq2seq(encoder_inputs[:bucket[0]], 
                                tag_inputs[:bucket[0]], decoder_inputs[:bucket[1]]) ### 
        outputs.append(bucket_outputs)
        lasthiddens.append(bucket_lasthiddens)### 
        if per_example_loss:
          losses.append(
              sequence_loss_by_example(
                  outputs[-1],
                  targets[:bucket[1]],
                  weights[:bucket[1]],
                  softmax_loss_function=softmax_loss_function))
        else:
          losses.append(
              sequence_loss(
                  outputs[-1],
                  targets[:bucket[1]],
                  weights[:bucket[1]],
                  softmax_loss_function=softmax_loss_function))

  return outputs, losses, lasthiddens###

def model_with_buckets(encoder_inputs,
                       decoder_inputs,
                       targets,
                       weights,
                       buckets,
                       seq2seq,
                       softmax_loss_function=None,
                       per_example_loss=False,
                       name=None):
  """Create a sequence-to-sequence model with support for bucketing.
  The seq2seq argument is a function that defines a sequence-to-sequence model,
  e.g., seq2seq = lambda x, y: basic_rnn_seq2seq(
      x, y, core_rnn_cell.GRUCell(24))
  Args:
    encoder_inputs: A list of Tensors to feed the encoder; first seq2seq input.
    decoder_inputs: A list of Tensors to feed the decoder; second seq2seq input.
    targets: A list of 1D batch-sized int32 Tensors (desired output sequence).
    weights: List of 1D batch-sized float-Tensors to weight the targets.
    buckets: A list of pairs of (input size, output size) for each bucket.
    seq2seq: A sequence-to-sequence model function; it takes 2 input that
      agree with encoder_inputs and decoder_inputs, and returns a pair
      consisting of outputs and states (as, e.g., basic_rnn_seq2seq).
    softmax_loss_function: Function (inputs-batch, labels-batch) -> loss-batch
      to be used instead of the standard softmax (the default if this is None).
    per_example_loss: Boolean. If set, the returned loss will be a batch-sized
      tensor of losses for each sequence in the batch. If unset, it will be
      a scalar with the averaged loss from all examples.
    name: Optional name for this operation, defaults to "model_with_buckets".
  Returns:
    A tuple of the form (outputs, losses), where:
      outputs: The outputs for each bucket. Its j'th element consists of a list
        of 2D Tensors. The shape of output tensors can be either
        [batch_size x output_size] or [batch_size x num_decoder_symbols]
        depending on the seq2seq model used.
      losses: List of scalar Tensors, representing losses for each bucket, or,
        if per_example_loss is set, a list of 1D batch-sized float Tensors.
  Raises:
    ValueError: If length of encoder_inputsut, targets, or weights is smaller
      than the largest (last) bucket.
  """
  if len(encoder_inputs) < buckets[-1][0]:
    raise ValueError("Length of encoder_inputs (%d) must be at least that of la"
                     "st bucket (%d)." % (len(encoder_inputs), buckets[-1][0]))
  if len(targets) < buckets[-1][1]:
    raise ValueError("Length of targets (%d) must be at least that of last"
                     "bucket (%d)." % (len(targets), buckets[-1][1]))
  if len(weights) < buckets[-1][1]:
    raise ValueError("Length of weights (%d) must be at least that of last"
                     "bucket (%d)." % (len(weights), buckets[-1][1]))

  all_inputs = encoder_inputs + decoder_inputs + targets + weights
  losses = []
  outputs = []
  ### output the last hidden state of encoder
  lasthiddens = []
  with ops.name_scope(name, "model_with_buckets", all_inputs):
    for j, bucket in enumerate(buckets):
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=True if j > 0 else None):
        bucket_outputs, _ , bucket_lasthiddens= seq2seq(encoder_inputs[:bucket[0]], 
                                decoder_inputs[:bucket[1]]) ### 
        outputs.append(bucket_outputs)
        lasthiddens.append(bucket_lasthiddens)### 
        if per_example_loss:
          losses.append(
              sequence_loss_by_example(
                  outputs[-1],
                  targets[:bucket[1]],
                  weights[:bucket[1]],
                  softmax_loss_function=softmax_loss_function))
        else:
          losses.append(
              sequence_loss(
                  outputs[-1],
                  targets[:bucket[1]],
                  weights[:bucket[1]],
                  softmax_loss_function=softmax_loss_function))

  return outputs, losses, lasthiddens###