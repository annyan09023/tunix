# Copyright 2025 Google LLC
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

"""Common test utilities."""

from collections.abc import Iterable
import dataclasses

from flax import nnx
import jax
import jax.numpy as jnp
import numpy as np
import qwix

import sentencepiece as spm


def assert_equal(path, x, y):
  np.testing.assert_array_equal(x, y, err_msg=f'Mismatch at path: {path}')


def assert_not_equal(path, x, y):
  np.testing.assert_(
      np.any(np.not_equal(x, y)), msg=f'Unexpected match at path: {path}'
  )


def assert_close(path, x, y, atol=1e-5, rtol=1e-5):
  np.testing.assert_allclose(
      x, y, atol, rtol, err_msg=f'Mismatch at path: {path}'
  )


class Decoder(nnx.Module):
  """Toy decoder for testing."""

  def __init__(self, rngs: nnx.Rngs):
    self.attn = nnx.MultiHeadAttention(
        num_heads=4,
        in_features=16,
        qkv_features=16,
        use_bias=False,
        decode=False,
        rngs=rngs,
    )
    kernel_init_fn = nnx.initializers.lecun_normal()
    self.w1 = nnx.Linear(
        in_features=16,
        out_features=32,
        rngs=rngs,
        kernel_init=nnx.with_partitioning(kernel_init_fn, ('fsdp', 'tp')),
    )
    self.w2 = nnx.Linear(
        in_features=32,
        out_features=16,
        rngs=rngs,
        kernel_init=nnx.with_partitioning(kernel_init_fn, ('tp', 'fsdp')),
    )

  def __call__(self, x):
    x = self.attn(x) + x
    h = nnx.relu(self.w1(x))
    h = self.w2(h) + x
    return h


@dataclasses.dataclass(kw_only=True, frozen=True)
class ModelConfig:
  """Model config for testing."""

  num_layers: int
  num_kv_heads: int
  head_dim: int


class ToyTransformer(nnx.Module):
  """Toy transformer for testing."""

  def __init__(
      self,
      rngs: nnx.Rngs,
      vocab_size: int = 256,
      num_layers: int = 4,
  ):
    self.config = ModelConfig(
        num_layers=num_layers, num_kv_heads=4, head_dim=16
    )
    self.emb = nnx.Embed(vocab_size, 16, rngs=rngs)
    self.layers = [Decoder(rngs=rngs) for _ in range(num_layers)]
    self.output = nnx.Linear(in_features=16, out_features=vocab_size, rngs=rngs)

  def __call__(
      self, x, positions, cache, attention_mask, output_hidden_states=False
  ):
    x = self.emb(x)
    for layer in self.layers:
      x = layer(x)
    if output_hidden_states:
      self.sow(
          nnx.Intermediate,
          'all_hidden_states',
          x,
      )
    return self.output(x), cache

  @property
  def num_embed(self) -> int:
    return self.emb.num_embeddings


class ToyScoreModel(ToyTransformer):
  """Toy transformer which outputs a score."""

  def __init__(
      self, rngs: nnx.Rngs, vocab_size: int = 256, num_layers: int = 4
  ):
    super().__init__(rngs, vocab_size, num_layers)

    self.score_layer = nnx.Linear(
        in_features=16,
        out_features=1,
        rngs=rngs,
    )

  def score(self, x, positions, attention_mask):
    x = self.emb(x)
    for layer in self.layers:
      x = layer(x)

    return self.score_layer(x)


def get_lora_model(
    model: nnx.Module,
    module_path: str = '.*w1|.*w2',
    mesh: jax.sharding.Mesh | None = None,
) -> nnx.Module:
  """Apply LoRA to ToyTransformer."""
  lora_provider = qwix.LoraProvider(
      module_path=module_path,
      rank=4,
      alpha=2.0,
  )
  dummy_model_input = {
      'x': jnp.ones((1, 1), dtype=jnp.int32),
      'positions': jnp.ones((1, 1), dtype=jnp.int32),
      'cache': None,
      'attention_mask': jnp.ones((1, 1, 1), dtype=jnp.bool),
  }
  lora_model = qwix.apply_lora_to_model(
      model, lora_provider, **dummy_model_input
  )
  if mesh is not None:
    with mesh:
      state = nnx.state(lora_model)
      pspecs = nnx.get_partition_spec(state)
      sharded_state = jax.lax.with_sharding_constraint(state, pspecs)
      nnx.update(lora_model, sharded_state)
  return lora_model


class MockVocab(spm.SentencePieceProcessor):
  """Mock vocabulary for testing."""

  def __init__(self):
    super().__init__()
    self._start_id = 3
    self._mapping_text_to_id = {
        '<pad>': 0,
        '<s>': 1,
        '</s>': 2,
        'input': 3,
        'string': 4,
        'hello': 5,
        'world': 6,
        'Hello': 7,
        'there': 8,
        '!': 9,
        'My': 10,
        'name': 11,
        'is': 12,
        'Morgane': 13,
    }
    self._vocab_size = len(self._mapping_text_to_id)

  def pad_id(self) -> int:
    return 0

  def bos_id(self) -> int:
    return 1

  def eos_id(self) -> int:
    return 2

  def GetPieceSize(self) -> int:  # pylint: disable=invalid-name
    return self._vocab_size

  def DecodeIds(self, ids: Iterable[int]) -> str:  # pylint: disable=invalid-name
    reverse_mapping = {v: k for k, v in self._mapping_text_to_id.items()}
    return ' '.join(reverse_mapping[e] for e in ids)

  def EncodeAsIds(self, text: str) -> list[int]:  # pylint: disable=invalid-name
    words = text.split(' ')
    return [self._mapping_text_to_id[word] for word in words]
