# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

# https://github.com/hkxIron/nmt

"""Basic sequence-to-sequence model with dynamic RNN support."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc

import tensorflow as tf

from tensorflow.python.layers import core as layers_core

from . import model_helper
from .utils import iterator_utils
from .utils import misc_utils as utils

utils.check_tensorflow_version()

__all__ = ["BaseModel", "Model"]


class BaseModel(object):
  """Sequence-to-sequence base class.
  """

  def __init__(self,
               hparams,
               mode,
               iterator,
               source_vocab_table,
               target_vocab_table,
               reverse_target_vocab_table=None,
               scope=None,
               extra_args=None):
    """Create the model.

    Args:
      hparams: Hyperparameter configurations.超参数配置
      mode: TRAIN | EVAL | INFER
      iterator: Dataset Iterator that feeds data.
      source_vocab_table: Lookup table mapping source words to ids.
      target_vocab_table: Lookup table mapping target words to ids.
      reverse_target_vocab_table: Lookup table mapping ids to target words. Only
        required in INFER mode. Defaults to None.
      scope: scope of the model.
      extra_args: model_helper.ExtraArgs, for passing customizable functions.

    """
    assert isinstance(iterator, iterator_utils.BatchedInput)
    self.iterator = iterator # database iterator
    self.mode = mode
    self.src_vocab_table = source_vocab_table
    self.tgt_vocab_table = target_vocab_table

    self.src_vocab_size = hparams.src_vocab_size
    self.tgt_vocab_size = hparams.tgt_vocab_size
    self.num_gpus = hparams.num_gpus
    self.time_major = hparams.time_major

    # extra_args: to make it flexible for adding external customizable code
    self.single_cell_fn = None
    if extra_args:
      self.single_cell_fn = extra_args.single_cell_fn

    # Set num layers
    self.num_encoder_layers = hparams.num_encoder_layers
    self.num_decoder_layers = hparams.num_decoder_layers
    assert self.num_encoder_layers
    assert self.num_decoder_layers

    # Set num residual layers
    if hasattr(hparams, "num_residual_layers"):  # compatible common_test_utils
      self.num_encoder_residual_layers = hparams.num_residual_layers
      self.num_decoder_residual_layers = hparams.num_residual_layers
    else:
      self.num_encoder_residual_layers = hparams.num_encoder_residual_layers
      self.num_decoder_residual_layers = hparams.num_decoder_residual_layers

    # Initializer
    initializer = model_helper.get_initializer(
        hparams.init_op, hparams.random_seed, hparams.init_weight)
    tf.get_variable_scope().set_initializer(initializer)

    # Embeddings
    self.init_embeddings(hparams, scope)
    self.batch_size = tf.size(self.iterator.source_sequence_length)

    # Projection
    """
    Lastly, we haven't mentioned projection_layer which is a dense matrix to turn 
    the top hidden states to logit vectors of dimension V
    """
    with tf.variable_scope(scope or "build_network"):
      with tf.variable_scope("decoder/output_projection"):
        self.output_layer = layers_core.Dense(
            hparams.tgt_vocab_size, use_bias=False, name="output_projection")

    ## Train graph
    res = self.build_graph(hparams, scope=scope)

    if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
      self.train_loss = res[1]
      self.word_count = tf.reduce_sum(
          self.iterator.source_sequence_length) + tf.reduce_sum(
              self.iterator.target_sequence_length)
    elif self.mode == tf.contrib.learn.ModeKeys.EVAL:
      self.eval_loss = res[1]
    elif self.mode == tf.contrib.learn.ModeKeys.INFER:
      self.infer_logits, _, self.final_context_state, self.sample_id = res
      self.sample_words = reverse_target_vocab_table.lookup(
          tf.to_int64(self.sample_id))

    if self.mode != tf.contrib.learn.ModeKeys.INFER:
      ## Count the number of predicted words for compute ppl.
      self.predict_count = tf.reduce_sum(
          self.iterator.target_sequence_length)

    self.global_step = tf.Variable(0, trainable=False)
    params = tf.trainable_variables()

    # Gradients and SGD update operation for training the model.
    # Arrage for the embedding vars to appear at the beginning.
    if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
      self.learning_rate = tf.constant(hparams.learning_rate)
      # warm-up
      self.learning_rate = self._get_learning_rate_warmup(hparams)
      # decay
      self.learning_rate = self._get_learning_rate_decay(hparams)

      # Optimizer
      if hparams.optimizer == "sgd":
        opt = tf.train.GradientDescentOptimizer(self.learning_rate)
        tf.summary.scalar("lr", self.learning_rate)
      elif hparams.optimizer == "adam":
        opt = tf.train.AdamOptimizer(self.learning_rate)

      # Gradients
      gradients = tf.gradients(
          self.train_loss,
          params,
          colocate_gradients_with_ops=hparams.colocate_gradients_with_ops)

      clipped_grads, grad_norm_summary, grad_norm = model_helper.gradient_clip(
          gradients, max_gradient_norm=hparams.max_gradient_norm)
      self.grad_norm = grad_norm

      self.update = opt.apply_gradients(
          zip(clipped_grads, params), global_step=self.global_step)

      # Summary
      self.train_summary = tf.summary.merge([
          tf.summary.scalar("lr", self.learning_rate),
          tf.summary.scalar("train_loss", self.train_loss),
      ] + grad_norm_summary)

    if self.mode == tf.contrib.learn.ModeKeys.INFER:
      self.infer_summary = self._get_infer_summary(hparams)

    # Saver
    self.saver = tf.train.Saver(
        tf.global_variables(), max_to_keep=hparams.num_keep_ckpts)

    # Print trainable variables
    utils.print_out("# Trainable variables")
    for param in params:
      utils.print_out("  %s, %s, %s" % (param.name, str(param.get_shape()),
                                        param.op.device))

  def _get_learning_rate_warmup(self, hparams):
    """Get learning rate warmup."""
    warmup_steps = hparams.warmup_steps
    warmup_scheme = hparams.warmup_scheme
    utils.print_out("  learning_rate=%g, warmup_steps=%d, warmup_scheme=%s" %
                    (hparams.learning_rate, warmup_steps, warmup_scheme))

    # Apply inverse decay if global steps less than warmup steps.
    # Inspired by https://arxiv.org/pdf/1706.03762.pdf (Section 5.3)
    # When step < warmup_steps,
    #   learing_rate *= warmup_factor ** (warmup_steps - step)
    if warmup_scheme == "t2t":
      # 0.01^(1/warmup_steps): we start with a lr, 100 times smaller
      warmup_factor = tf.exp(tf.log(0.01) / warmup_steps) # warmup_factor < 1
      # warmup时, inv_decay会越来越大
      inv_decay = warmup_factor**(
          tf.to_float(warmup_steps - self.global_step))
    else:
      raise ValueError("Unknown warmup scheme %s" % warmup_scheme)

    return tf.cond(
        self.global_step < hparams.warmup_steps,
        lambda: inv_decay * self.learning_rate,
        lambda: self.learning_rate,
        name="learning_rate_warump_cond")

  def _get_learning_rate_decay(self, hparams):
    """Get learning rate decay."""
    if hparams.decay_scheme in ["luong5", "luong10", "luong234"]:
      decay_factor = 0.5
      if hparams.decay_scheme == "luong5":
        start_decay_step = int(hparams.num_train_steps / 2)
        decay_times = 5
      elif hparams.decay_scheme == "luong10":
        start_decay_step = int(hparams.num_train_steps / 2)
        decay_times = 10
      elif hparams.decay_scheme == "luong234":
        start_decay_step = int(hparams.num_train_steps * 2 / 3)
        decay_times = 4
      remain_steps = hparams.num_train_steps - start_decay_step
      decay_steps = int(remain_steps / decay_times)
    elif not hparams.decay_scheme:  # no decay
      start_decay_step = hparams.num_train_steps
      decay_steps = 0
      decay_factor = 1.0
    elif hparams.decay_scheme:
      raise ValueError("Unknown decay scheme %s" % hparams.decay_scheme)
    utils.print_out("  decay_scheme=%s, start_decay_step=%d, decay_steps %d, "
                    "decay_factor %g" % (hparams.decay_scheme,
                                         start_decay_step,
                                         decay_steps,
                                         decay_factor))

    return tf.cond(
        self.global_step < start_decay_step,
        lambda: self.learning_rate,
        lambda: tf.train.exponential_decay(
            self.learning_rate,
            (self.global_step - start_decay_step),
            decay_steps, decay_factor, staircase=True),
        name="learning_rate_decay_cond")

  def init_embeddings(self, hparams, scope):
    """Init embeddings."""
    self.embedding_encoder, self.embedding_decoder = (
        model_helper.create_emb_for_encoder_and_decoder(
            share_vocab=hparams.share_vocab,
            src_vocab_size=self.src_vocab_size,
            tgt_vocab_size=self.tgt_vocab_size,
            src_embed_size=hparams.num_units,
            tgt_embed_size=hparams.num_units,
            num_partitions=hparams.num_embeddings_partitions,
            src_vocab_file=hparams.src_vocab_file,
            tgt_vocab_file=hparams.tgt_vocab_file,
            src_embed_file=hparams.src_embed_file,
            tgt_embed_file=hparams.tgt_embed_file,
            scope=scope,
        )
    )

  def train(self, sess):
    assert self.mode == tf.contrib.learn.ModeKeys.TRAIN
    return sess.run([self.update,
                     self.train_loss,
                     self.predict_count,
                     self.train_summary,
                     self.global_step,
                     self.word_count,
                     self.batch_size,
                     self.grad_norm,
                     self.learning_rate])

  def eval(self, sess):
    assert self.mode == tf.contrib.learn.ModeKeys.EVAL
    return sess.run([self.eval_loss,
                     self.predict_count,
                     self.batch_size])

  def build_graph(self, hparams, scope=None):
    """Subclass must implement this method.

    Creates a sequence-to-sequence model with dynamic RNN decoder API.
    Args:
      hparams: Hyperparameter configurations.
      scope: VariableScope for the created subgraph; default "dynamic_seq2seq".

    Returns:
      A tuple of the form (logits, loss, final_context_state),
      where:
        logits: float32 Tensor [batch_size x num_decoder_symbols].
        loss: the total loss / batch_size.
        final_context_state: The final state of decoder RNN.

    Raises:
      ValueError: if encoder_type differs from mono and bi, or
        attention_option is not (luong | scaled_luong |
        bahdanau | normed_bahdanau).
    """
    utils.print_out("# creating %s graph ..." % self.mode)
    dtype = tf.float32

    with tf.variable_scope(scope or "dynamic_seq2seq", dtype=dtype):
      # Encoder
      encoder_outputs, encoder_state = self._build_encoder(hparams)

      ## Decoder
      # logits: size [time, batch_size, vocab_size] when time_major=True.
      logits, sample_id, final_context_state = self._build_decoder(
          encoder_outputs, encoder_state, hparams)

      ## Loss
      if self.mode != tf.contrib.learn.ModeKeys.INFER:
        with tf.device(model_helper.get_device_str(self.num_encoder_layers - 1,
                                                   self.num_gpus)):
          # train阶段需要计算loss
          loss = self._compute_loss(logits)
      else:
        loss = None

      return logits, loss, final_context_state, sample_id

  @abc.abstractmethod
  def _build_encoder(self, hparams): # 静态方法,此处接口没有规定返回值,只能说python的接口好随意
    """Subclass must implement this.

    Build and run an RNN encoder.

    Args:
      hparams: Hyperparameters configurations.

    Returns:
      A tuple of encoder_outputs and encoder_state.
    """
    pass

  def _build_encoder_cell(self, hparams, num_layers, num_residual_layers,
                          base_gpu=0):
    """Build a multi-layer RNN cell that can be used by encoder."""

    return model_helper.create_rnn_cell(
        unit_type=hparams.unit_type,
        num_units=hparams.num_units,
        num_layers=num_layers,
        num_residual_layers=num_residual_layers,
        forget_bias=hparams.forget_bias,
        dropout=hparams.dropout,
        num_gpus=hparams.num_gpus,
        mode=self.mode,
        base_gpu=base_gpu,
        single_cell_fn=self.single_cell_fn)

  def _get_infer_maximum_iterations(self, hparams, source_sequence_length):
    """Maximum decoding steps at inference time."""
    if hparams.tgt_max_len_infer:
      maximum_iterations = hparams.tgt_max_len_infer
      utils.print_out("  decoding maximum_iterations %d" % maximum_iterations)
    else:
      # TODO(thangluong): add decoding_length_factor flag
      # 最多是encoder的2倍长度
      decoding_length_factor = 2.0
      max_encoder_length = tf.reduce_max(source_sequence_length)
      maximum_iterations = tf.to_int32(tf.round(
          tf.to_float(max_encoder_length) * decoding_length_factor))
    return maximum_iterations

  def _build_decoder(self, encoder_outputs, encoder_state, hparams):
    """Build and run a RNN decoder with a final projection layer.

    Args:
      encoder_outputs: The outputs of encoder for every time step.
      encoder_state: The final state of the encoder.
      hparams: The Hyperparameters configurations.

    Returns:
      A tuple of final logits and final decoder state:
        logits: size [time, batch_size, vocab_size] when time_major=True.
    """
    # 将 word -> id
    tgt_sos_id = tf.cast(self.tgt_vocab_table.lookup(tf.constant(hparams.sos)),
                         tf.int32)
    tgt_eos_id = tf.cast(self.tgt_vocab_table.lookup(tf.constant(hparams.eos)),
                         tf.int32)
    iterator = self.iterator

    # maximum_iteration: The maximum decoding steps.
    maximum_iterations = self._get_infer_maximum_iterations(
        hparams, iterator.source_sequence_length)

    ## Decoder.
    with tf.variable_scope("decoder") as decoder_scope:
      # _builder_decoder_cell 待子类实现
      # 对于attention model,将会用attention_cell重写此cell
      cell, decoder_initial_state = self._build_decoder_cell(
          hparams, encoder_outputs, encoder_state,
          iterator.source_sequence_length)

      ## Train or eval
      if self.mode != tf.contrib.learn.ModeKeys.INFER:
        # target_input:[batch_size, max_time]
        target_input = iterator.target_input
        if self.time_major:
          # target_input:[max_time, batch_size]
          target_input = tf.transpose(target_input)
        # embedding_decoder: [vocab_size, embedding_size=(num_units)]
        # decoder_emp_inp: [max_time, batch_size, num_units]
        decoder_emb_inp = tf.nn.embedding_lookup(self.embedding_decoder, target_input)

        """
        By separating out decoders and helpers, we can reuse different codebases, 
        e.g., TrainingHelper can be substituted with GreedyEmbeddingHelper to do greedy decoding.
        """
        # Helper
        # decoder_embed_inp: [max_time, batch, num_units=embedding_size]
        # sequence_length: [batch]
        helper = tf.contrib.seq2seq.TrainingHelper(
            inputs = decoder_emb_inp,
            sequence_length = iterator.target_sequence_length,
            time_major=self.time_major)

        # Decoder
        # 注意 (NOTE):decoder部分中的initial_state需要以encoder中的最后一个hidden_state作为初始输入,这个是seq2seq的关键
        my_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell=cell, # 定义decoder里的rnn cell
            helper=helper, # helper定义decoder里的embedding输入
            initial_state=decoder_initial_state,) # decoder里的hidden,cell初始化状态
            # 注意,此处并没有定义 output_layer

        """
        dynamic_decode
        用于构造一个动态的decoder，返回的内容是：(final_outputs, final_state, final_sequence_lengths).
        其中，final_outputs是一个namedtuple，里面包含两项(rnn_outputs, sample_id)
        rnn_output: [batch_size, decoder_targets_length, vocab_size]，保存decode每个时刻每个单词的概率，可以用来计算loss
        sample_id: [batch_size, decoder_targets_length], tf.int32，保存最终的解码结果。可以表示最后的答案
        """
        # Dynamic decoding
        decoder_outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder=my_decoder,
            output_time_major=self.time_major,
            swap_memory=True,
            scope=decoder_scope)

        sample_id = decoder_outputs.sample_id

        # Note: there's a subtle difference here between train and inference.
        # We could have set output_layer when create my_decoder
        #   and shared more code between train and inference.
        # We chose to apply the output_layer to all timesteps for speed:
        #   10% improvements for small models & 20% for larger ones.
        # If memory is a concern, we should apply output_layer per timestep.

        # logits: [batch_size, decoder_targets_length, vocab_size]
        logits = self.output_layer(decoder_outputs.rnn_output)

      ## Inference
      else:
        beam_width = hparams.beam_width
        length_penalty_weight = hparams.length_penalty_weight
        # 开始符, start of sentence
        start_tokens = tf.fill(dims=[self.batch_size], value=tgt_sos_id)
        end_token = tgt_eos_id

        if beam_width > 0:
          # beam search
          my_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
              cell=cell,
              embedding=self.embedding_decoder,
              start_tokens=start_tokens,
              end_token=end_token,
              initial_state=decoder_initial_state, # decoder时,输入的初始化状态
              beam_width=beam_width,
              output_layer=self.output_layer,
              length_penalty_weight=length_penalty_weight)
        else:
          # Helper
          sampling_temperature = hparams.sampling_temperature
          if sampling_temperature > 0.0:
            """
            预测时helper，继承自GreedyEmbeddingHelper，
            下一时刻输入是上一时刻通过某种概率分布采样而来在经过embedding之后的向量
            
            softmax_temperature: (Optional) float32 scalar, 
            value to divide the logits by before computing the softmax.
            Larger values (above 1.0) result in more random samples, 
            while smaller values push the sampling distribution towards the argmax. 
            Must be strictly greater than 0. Defaults to 1.0.
            """
            helper = tf.contrib.seq2seq.SampleEmbeddingHelper(
                embedding=self.embedding_decoder,
                start_tokens=start_tokens, # int32 vector shaped [batch_size], the start tokens.
                end_token=end_token, # int32 scalar, the token that marks end of decoding.
                softmax_temperature=sampling_temperature,
                seed=hparams.random_seed)
          else:
            """
            测试阶段,我们使用GreedySearch
            
            Here, we use GreedyEmbeddingHelper instead of TrainingHelper. 
            Since we do not know the target sequence lengths in advance, 
            we use maximum_iterations to limit the translation lengths. 
            One heuristic is to decode up to two times the source sentence lengths.
            """
            helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
                embedding=self.embedding_decoder,
                start_tokens=start_tokens, # int32 vector shaped [batch_size], the start tokens.
                end_token=end_token) # int32 scalar, the token that marks end of decoding.

          # Decoder
          # encoder_state: [hidden = [batch, hidden_size], cell = [batch, hidden_size]]
          # output_layer:[batch, decoder_output_length, target_vocab_size]
          my_decoder = tf.contrib.seq2seq.BasicDecoder(
              cell=cell,
              helper=helper,
              initial_state=decoder_initial_state,
              output_layer=self.output_layer  # applied per timestep
          )

        # Dynamic decoding
        # decoder_output:(rnn_output:[batch_size, decoder_targets_length, vocab_size],
        #                            sample_id:[batch_size, decoder_targets_length])
        decoder_outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder=my_decoder,
            maximum_iterations=maximum_iterations,
            output_time_major=self.time_major,
            swap_memory=True,
            scope=decoder_scope)

        if beam_width > 0:
          # 不明白为何no_op()是logits,可能是如果为beam_search, logits估计没有什么意义吧
          logits = tf.no_op() # Does nothing. Only useful as a placeholder for control edges.
          sample_id = decoder_outputs.predicted_ids
        else:
          logits = decoder_outputs.rnn_output
          sample_id = decoder_outputs.sample_id

    return logits, sample_id, final_context_state

  def get_max_time(self, tensor):
    time_axis = 0 if self.time_major else 1
    return tensor.shape[time_axis].value or tf.shape(tensor)[time_axis]

  @abc.abstractmethod
  def _build_decoder_cell(self, hparams, encoder_outputs, encoder_state,
                          source_sequence_length):
    """Subclass must implement this.

    Args:
      hparams: Hyperparameters configurations.
      encoder_outputs: The outputs of encoder for every time step.
      encoder_state: The final state of the encoder.
      source_sequence_length: sequence length of encoder_outputs.

    Returns:
      A tuple of a multi-layer RNN cell used by decoder
        and the intial state of the decoder RNN.
    """
    pass

  def _compute_loss(self, logits):
    """Compute optimization loss."""
    # target_output:[batch, max_time]
    target_output = self.iterator.target_output
    if self.time_major:
      target_output = tf.transpose(target_output)
    max_time = self.get_max_time(target_output)

    """
    Here, target_weights is a zero-one matrix of the same size as decoder_outputs. 
    It masks padding positions outside of the target sequence lengths with values 0.
    
    Important note: It's worth pointing out that we divide the loss by batch_size, 
    so our hyperparameters are "invariant" to batch_size. 
    Some people divide the loss by (batch_size * num_time_steps), 
    which plays down(淡化) the errors made on short sentences. (有点不明白原因)
    
    More subtly, 
    our hyperparameters (applied to the former way) can't be used for the latter way. 
    For example, if both approaches use SGD with a learning of 1.0, the latter approach effectively 
    uses a much smaller learning rate of 1 / num_time_steps.
    
    google的这段注释的确精辟
    """
    crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=target_output, logits=logits)
    # target_sequence_length:[batch, target_sequence_length]
    # target_sequence_mask:[batch, max_target_sequence_length]
    target_weights = tf.sequence_mask(
        lengths=self.iterator.target_sequence_length,
        maxlen=max_time, # max_time = max_target_sequence_length
        dtype=logits.dtype)
    if self.time_major:
      target_weights = tf.transpose(target_weights)

    loss = tf.reduce_sum(input_tensor=crossent * target_weights, axis=None) / tf.to_float(self.batch_size)
    return loss

  def _get_infer_summary(self, hparams):
    return tf.no_op()

  def infer(self, sess):
    assert self.mode == tf.contrib.learn.ModeKeys.INFER
    return sess.run([
        self.infer_logits,
        self.infer_summary,
        self.sample_id,
        self.sample_words
    ])

  def decode(self, sess):
    """Decode a batch.

    Args:
      sess: tensorflow session to use.

    Returns:
      A tuple consiting of outputs, infer_summary.
        outputs: of size [batch_size, time]
    """
    _, infer_summary, _, sample_words = self.infer(sess)

    # make sure outputs is of shape [batch_size, time] or [beam_width,
    # batch_size, time] when using beam search.
    if self.time_major:
      sample_words = sample_words.transpose()
    elif sample_words.ndim == 3:  # beam search output in [batch_size,
                                  # time, beam_width] shape.
      # [beam_width, batch_size, time]
      sample_words = sample_words.transpose([2, 0, 1])
    return sample_words, infer_summary


class Model(BaseModel):
  """Sequence-to-sequence dynamic model.

  This class implements a multi-layer recurrent neural network as encoder,
  and a multi-layer recurrent neural network decoder.
  """

  def _build_encoder(self, hparams):
    """Build an encoder."""
    num_layers = self.num_encoder_layers
    num_residual_layers = self.num_encoder_residual_layers
    iterator = self.iterator

    source = iterator.source
    if self.time_major: # 是否时间优先
      source = tf.transpose(a=source) # source:[batch, max_time] -> [max_time, batch]

    with tf.variable_scope("encoder") as scope:
      dtype = scope.dtype
      # Look up embedding
      # embedding_encoder: [src_vocab_size, embedding_size]
      # source: [max_time, batch_size]
      # encoder_emp_inp: [max_time, batch_size, embedding_size]
      encoder_emb_inp = tf.nn.embedding_lookup(params=self.embedding_encoder, ids=source)

      # Encoder_outputs: [max_time, batch_size, num_units]
      if hparams.encoder_type == "uni": # 单向
        utils.print_out("  num_layers = %d, num_residual_layers=%d" %
                        (num_layers, num_residual_layers))
        cell = self._build_encoder_cell(
            hparams, num_layers, num_residual_layers)
        # encoder_output:[batch, source_sequence_length, hidden_size]
        # encoder_state:[hidden=[batch, hidden_size], cell=[batch, hidden_size]]
        encoder_outputs, encoder_state = tf.nn.dynamic_rnn(
            cell=cell,
            inputs=encoder_emb_inp,
            dtype=dtype,
            sequence_length=iterator.source_sequence_length,
            time_major=self.time_major,
            swap_memory=True)
      elif hparams.encoder_type == "bi": # 双向
        num_bi_layers = int(num_layers / 2)
        num_bi_residual_layers = int(num_residual_layers / 2)
        utils.print_out("  num_bi_layers = %d, num_bi_residual_layers=%d" %
                        (num_bi_layers, num_bi_residual_layers))

        # encoder_outputs: [batch, input_sequence_length, 2*num_units]
        # bi_encoder_state: (states_fw, states_bw)
        # cell_state_fw: ([batch, num_units], [batch, num_units], ... , [batch, num_units])
        # hidden_state_fw: ([batch, num_units],[batch, num_units], ..., [batch, num_units])
        encoder_outputs, bi_encoder_state = (
            self._build_bidirectional_rnn(
                inputs=encoder_emb_inp,
                sequence_length=iterator.source_sequence_length,
                dtype=dtype,
                hparams=hparams,
                num_bi_layers=num_bi_layers,
                num_bi_residual_layers=num_bi_residual_layers)
        )

        if num_bi_layers == 1:
          encoder_state = bi_encoder_state
        else:
          # alternatively concat forward and backward states
          encoder_state = []
          for layer_id in range(num_bi_layers): # 将每一层的前向hidden vector与后向hidden vector拼接起来
            encoder_state.append(bi_encoder_state[0][layer_id])  # forward
            encoder_state.append(bi_encoder_state[1][layer_id])  # backward
          # 将多个layer的LSTMStateTuple拼接起来
          encoder_state = tuple(encoder_state) # 将list转为tuple
      else:
        raise ValueError("Unknown encoder_type %s" % hparams.encoder_type)
    return encoder_outputs, encoder_state

  def _build_bidirectional_rnn(self,
                               inputs,
                               sequence_length,
                               dtype,
                               hparams,
                               num_bi_layers,
                               num_bi_residual_layers,
                               base_gpu=0):
    """Create and call biddirectional RNN cells.

    Args:
      num_residual_layers: Number of residual layers from top to bottom. For
        example, if `num_bi_layers=4` and `num_residual_layers=2`, the last 2 RNN
        layers in each RNN cell will be wrapped with `ResidualWrapper`.

      base_gpu: The gpu device id to use for the first forward RNN layer. The
        i-th forward RNN layer will use `(base_gpu + i) % num_gpus` as its
        device id. The `base_gpu` for backward RNN cell is `(base_gpu +
        num_bi_layers)`.

    Returns:
      The concatenated bidirectional output and the bidirectional RNN cell"s
      state.
    """

    # Construct forward and backward cells
    fw_cell = self._build_encoder_cell(hparams,
                                       num_bi_layers,
                                       num_bi_residual_layers,
                                       base_gpu=base_gpu)
    bw_cell = self._build_encoder_cell(hparams,
                                       num_bi_layers,
                                       num_bi_residual_layers,
                                       base_gpu=(base_gpu + num_bi_layers))
    """
    (output_fw, output_bw), (states_fw, states_bw) = tf.nn.bidirectional_dynamic_rnn
    
    output_fw: [batch, input_sequence_length, num_units],它的值为hidden_state
    output_bw: [batch, input_sequence_length, num_units],它的值为hidden_state
    
    (cell_state_fw, hidden_state_fw) = states_fw
    cell_state: [batch, num_units]
    hidden_state: [batch, num_units]
    states_fw: (LSTMTuple(cell_state_fw, hidden_state_fw),LSTMTuple(cell_state_fw, hidden_state_fw), ... ),
    rnn有n层,state_fw 的tuple的长度就会为n
    
    """
    bi_outputs, bi_state = tf.nn.bidirectional_dynamic_rnn(
        cell_fw=fw_cell,
        cell_bw=bw_cell,
        inputs=inputs,
        dtype=dtype,
        sequence_length=sequence_length,
        time_major=self.time_major,
        swap_memory=True)

    # bi_outputs: [batch, input_sequence_length, 2*num_units]
    return tf.concat(bi_outputs, axis=-1), bi_state

  def _build_decoder_cell(self, hparams, encoder_outputs, encoder_state,
                          source_sequence_length):
    """Build an RNN cell that can be used by decoder."""
    # We only make use of encoder_outputs in attention-based models
    if hparams.attention:
      raise ValueError("BasicModel doesn't support attention.")

    cell = model_helper.create_rnn_cell(
        unit_type=hparams.unit_type,
        num_units=hparams.num_units,
        num_layers=self.num_decoder_layers,
        num_residual_layers=self.num_decoder_residual_layers,
        forget_bias=hparams.forget_bias,
        dropout=hparams.dropout,
        num_gpus=self.num_gpus,
        mode=self.mode,
        single_cell_fn=self.single_cell_fn)

    # For beam search, we need to replicate encoder infos beam_width times
    if self.mode == tf.contrib.learn.ModeKeys.INFER and hparams.beam_width > 0:
      decoder_initial_state = tf.contrib.seq2seq.tile_batch(
          t=encoder_state, multiplier=hparams.beam_width)
    else:
      decoder_initial_state = encoder_state

    return cell, decoder_initial_state
