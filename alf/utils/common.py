# Copyright (c) 2019 Horizon Robotics. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Various functions used by different alf modules."""

import collections
from collections import OrderedDict
import glob
import math
import os
import shutil
from typing import Callable

from absl import flags
from absl import logging
import gin
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from tf_agents.specs import tensor_spec
from tf_agents.trajectories.time_step import StepType
from tf_agents.utils import common as tfa_common
from tf_agents.specs.distribution_spec import DistributionSpec

from alf.utils import summary_utils, gin_utils
from alf.utils.conditional_ops import conditional_update, run_if, select_from_mask


def namedtuple(typename, field_names, default_value=None, default_values=()):
    """namedtuple with default value.

    Args:
        typename (str): type name of this namedtuple
        field_names (list[str]): name of each field
        default_value (Any): the default value for all fields
        default_values (list|dict): default value for each field
    Returns:
        the type for the namedtuple
    """
    T = collections.namedtuple(typename, field_names)
    T.__new__.__defaults__ = (default_value, ) * len(T._fields)
    if isinstance(default_values, collections.Mapping):
        prototype = T(**default_values)
    else:
        prototype = T(*default_values)
    T.__new__.__defaults__ = tuple(prototype)
    return T


class ActionTimeStep(
        namedtuple(
            'ActionTimeStep',
            ['step_type', 'reward', 'discount', 'observation', 'prev_action'])
):
    """TimeStep with action."""

    def is_first(self):
        if tf.is_tensor(self.step_type):
            return tf.equal(self.step_type, StepType.FIRST)
        return np.equal(self.step_type, StepType.FIRST)

    def is_mid(self):
        if tf.is_tensor(self.step_type):
            return tf.equal(self.step_type, StepType.MID)
        return np.equal(self.step_type, StepType.MID)

    def is_last(self):
        if tf.is_tensor(self.step_type):
            return tf.equal(self.step_type, StepType.LAST)
        return np.equal(self.step_type, StepType.LAST)


def make_action_time_step(time_step, prev_action):
    return ActionTimeStep(
        step_type=time_step.step_type,
        reward=time_step.reward,
        discount=time_step.discount,
        observation=time_step.observation,
        prev_action=prev_action)


LossInfo = namedtuple(
    "LossInfo",
    [
        "loss",  # batch loss shape should be (T, B)
        "scalar_loss",  # shape is ()
        "extra"  # nested batch and/or scalar losses, for summary only
    ],
    default_value=())


def zero_tensor_from_nested_spec(nested_spec, batch_size):
    def _zero_tensor(spec):
        if batch_size is None:
            shape = spec.shape
        else:
            spec_shape = tf.convert_to_tensor(value=spec.shape, dtype=tf.int32)
            shape = tf.concat(([batch_size], spec_shape), axis=0)
        dtype = spec.dtype
        return tf.zeros(shape, dtype)

    return tf.nest.map_structure(_zero_tensor, nested_spec)


def set_per_process_memory_growth(flag=True):
    """Set if memory growth should be enabled for a PhysicalDevice.

    With memory growth set to True, tf will not allocate all memory on the
    device upfront.

    Args:
        flag (bool): True if do not allocate memory upfront.
    """
    gpus = tf.config.experimental.list_physical_devices('GPU')
    if gpus:
        for gpu in gpus:
            try:
                tf.config.experimental.set_memory_growth(gpu, flag)
            except RuntimeError as e:
                # Memory growth must be set at program startup
                print(e)


def as_list(x):
    """Convert x to a list.

    It performs the following conversion:
        None => []
        list => x
        tuple => list(x)
        other => [x]
    Args:
        x (any): the object to be converted
    Returns:
        a list.
    """
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def get_target_updater(models, target_models, tau=1.0, period=1):
    """Performs a soft update of the target model parameters.

    For each weight w_s in the model, and its corresponding
    weight w_t in the target_model, a soft update is:
    w_t = (1 - tau) * w_t + tau * w_s

    Args:
        models (Network | list[Network]): the current model.
        target_models (Network | list[Network]): the model to be updated.
        tau (float): A float scalar in [0, 1]. Default `tau=1.0` means hard
            update.
        period (int): Step interval at which the target model is updated.

    Returns:
        A callable that performs a soft update of the target model parameters.
    """
    models = as_list(models)
    target_models = as_list(target_models)

    def update():
        update_ops = []
        for model, target_model in zip(models, target_models):
            update_op = tfa_common.soft_variables_update(
                model.variables, target_model.variables, tau)
            update_ops.append(update_op)
        return tf.group(*update_ops)

    return tfa_common.Periodically(update, period, 'periodic_update_targets')


def add_nested_summaries(prefix, data):
    """Add summary about loss_info

    Args:
        prefix (str): the prefix of the names of the summaries
        data (dict or namedtuple): data to be summarized
    """
    fields = data.keys() if isinstance(data, dict) else data._fields
    for field in fields:
        elem = data[field] if isinstance(data, dict) else getattr(data, field)
        name = prefix + '/' + field
        if isinstance(elem, dict) or (isinstance(elem, tuple)
                                      and hasattr(elem, '_fields')):
            add_nested_summaries(name, elem)
        elif isinstance(elem, tf.Tensor):
            tf.summary.scalar(name, elem)


def add_loss_summaries(loss_info: LossInfo):
    """Add summary about loss_info

    Args:
        loss_info (LossInfo): loss_info.extra must be a namedtuple
    """
    tf.summary.scalar('loss', data=loss_info.loss)
    if not loss_info.extra:
        return
    if not hasattr(loss_info.extra, '_fields'):
        # not a namedtuple
        return
    add_nested_summaries('loss', loss_info.extra)


def add_action_summaries(actions, action_specs):
    """Generate histogram summaries for actions.

    Actions whose rank is more than 1 will be skipped.

    Args:
        actions (nested Tensor): actions to be summarized
        action_specs (nested TensorSpec): spec for the actions
    """
    action_specs = tf.nest.flatten(action_specs)
    actions = tf.nest.flatten(actions)

    for i, (action, action_spec) in enumerate(zip(actions, action_specs)):
        if len(action_spec.shape) > 1:
            continue

        if tensor_spec.is_discrete(action_spec):
            summary_utils.histogram_discrete(
                name="action/%s" % i,
                data=action,
                bucket_min=action_spec.minimum,
                bucket_max=action_spec.maximum)
        else:
            if len(action_spec.shape) == 0:
                action_dim = 1
            else:
                action_dim = action_spec.shape[-1]
            action = tf.reshape(action, (-1, action_dim))

            def _get_val(a, i):
                return a if len(a.shape) == 0 else a[i]

            for a in range(action_dim):
                # TODO: use a descriptive name for the summary
                summary_utils.histogram_continuous(
                    name="action/%s/%s" % (i, a),
                    data=action[:, a],
                    bucket_min=_get_val(action_spec.minimum, a),
                    bucket_max=_get_val(action_spec.maximum, a))


def get_distribution_params(nested_distribution):
    """Get the params for an optionally nested action distribution.

    Only returns parameters that have tf.Tensor values.

    Args:
        nested_distribution (nested tf.distribution.Distribution):
            The distributions whose parameter tensors to extract.
    Returns:
        A nest of distribution parameters. Each leaf is a dict corresponding to
        one distribution, with keys as parameter name and values as tensors
        containing parameter values.
    """

    def _tensor_parameters_only(params):
        return {
            k: params[k]
            for k in params if isinstance(params[k], tf.Tensor)
        }

    return tf.nest.map_structure(
        lambda single_dist: _tensor_parameters_only(single_dist.parameters),
        nested_distribution)


def concat_shape(shape1, shape2):
    """Concatenate two shape tensor.

    Args:
        shape1 (Tensor|list): first shape
        shape2 (Tensor|list): second shape
    Returns:
        Tensor for the concatenated shape
    """
    if not isinstance(shape1, tf.Tensor):
        shape1 = tf.convert_to_tensor(shape1, dtype=tf.int32)
    if not isinstance(shape2, tf.Tensor):
        shape2 = tf.convert_to_tensor(shape2, dtype=tf.int32)
    return tf.concat([shape1, shape2], axis=0)


def expand_dims_as(x, y):
    """Expand the shape of `x` with extra singular dimensions.

    The result is broadcastable to the shape of `y`
    Args:
        x (Tensor): source tensor
        y (Tensor): target tensor. Only its shape will be used.
    Returns:
        x with extra singular dimensions.
    """
    assert len(x.shape) <= len(y.shape)
    tf.assert_equal(tf.shape(x), tf.shape(y)[:len(x.shape)])
    k = len(y.shape) - len(x.shape)
    if k == 0:
        return x
    else:
        return tf.reshape(x, concat_shape(tf.shape(x), [1] * k))


def reset_state_if_necessary(state, initial_state, reset_mask):
    """Reset state to initial state according to reset_mask

    Args:
        state (nested Tensor): the current batched states
        initial_state (nested Tensor): batched intitial states
        reset_mask (nested Tensor): with shape=(batch_size,), dtype=tf.bool
    Returns:
        nested Tensor
    """
    return tf.nest.map_structure(
        lambda i_s, s: tf.where(expand_dims_as(reset_mask, i_s), i_s, s),
        initial_state, state)


_summary_enabled_var = None


def _get_summary_enabled_var():
    global _summary_enabled_var
    if _summary_enabled_var is None:
        _summary_enabled_var = tf.Variable(
            True, dtype=tf.bool, name="summary_enabled")
    return _summary_enabled_var


def enable_summary(flag):
    """Enable or disable summary.

    Args:
        flag (bool): True to enable, False to disable
    """
    v = _get_summary_enabled_var()
    v.assign(flag)


def is_summary_enabled():
    """Return whether summary is enabled."""
    return _get_summary_enabled_var()


def run_under_record_context(func,
                             summary_dir,
                             summary_interval,
                             flush_millis,
                             summary_max_queue=10):
    """Run `func` under summary record context.

    Args:
        func (Callable): the function to be executed.
        summary_dir (str): directory to store summary. A directory starting with
            "~/" will be expanded to "$HOME/"
        summary_interval (int): how often to generate summary based on the
            global counter
        flush_millis (int): flush summary to disk every so many milliseconds
        summary_max_queue (int): the largest number of summaries to keep in a queue; will
          flush once the queue gets bigger than this. Defaults to 10.
    """

    import alf.utils.summary_utils
    summary_dir = os.path.expanduser(summary_dir)
    summary_writer = tf.summary.create_file_writer(
        summary_dir, flush_millis=flush_millis, max_queue=summary_max_queue)
    summary_writer.set_as_default()
    global_step = get_global_counter()
    with tf.summary.record_if(lambda: tf.logical_and(
            is_summary_enabled(),
            tf.equal((global_step + 1) % summary_interval, 0))):
        func()


from tensorflow.python.ops.summary_ops_v2 import should_record_summaries


def get_global_counter(default_counter=None):
    """Get the global counter.

    Args:
        default_counter (Variable): If not None, this counter will be returned.
    Returns:
        If default_counter is not None, it will be returned. Otherwise,
        if tf.summary.experimental.get_step() is not None, it will be returned.
        Othewise, a counter will be created and returned.
        tf.summary.experimental.set_step() will be set to the created counter.

    """
    if default_counter is None:
        default_counter = tf.summary.experimental.get_step()
        if default_counter is None:
            default_counter = tf.Variable(
                0, dtype=tf.int64, trainable=False, name="global_counter")
            tf.summary.experimental.set_step(default_counter)
    return default_counter


@gin.configurable
def cast_transformer(observation, dtype=tf.float32):
    """Cast observation

    Args:
         observation (nested Tensor): observation
         dtype (Dtype): The destination type.
    Returns:
        casted observation
    """

    def _cast(obs):
        if isinstance(obs, tf.Tensor):
            return tf.cast(obs, dtype)
        return obs

    return tf.nest.map_structure(_cast, observation)


@gin.configurable
def image_scale_transformer(observation, fields=None, min=-1.0, max=1.0):
    """Scale image to min and max (0->min, 255->max)

    Args:
        observation (nested Tensor): If observation is a nested structure, only
            namedtuple and dict are supported for now.
        fields (list[str]): the fields to be applied with the transformation. If
            None, then `observation` must be a tf.Tensor with dtype uint8. A
            field str can be a multi-step path denoted by "A.B.C".
        min (float): normalize minimum to this value
        max (float): normalize maximum to this value
    Returns:
        Transfromed observation
    """

    def _transform_image(obs):
        assert isinstance(obs, tf.Tensor), str(type(obs)) + ' is not Tensor'
        assert obs.dtype == tf.uint8, "Image must have dtype uint8!"
        obs = tf.cast(obs, tf.float32)
        return ((max - min) / 255.) * obs + min

    def _traverse_path(obs, path):
        """Traverse `path` and transform the image at the path end."""
        if not path:
            return _transform_image(obs)
        step = path[0]
        if isinstance(obs, tuple) and hasattr(obs, '_fields'):
            new_val = _traverse_path(getattr(obs, step), path[1:])
            return obs._replace(**{step: new_val})
        elif isinstance(obs, dict):
            new_obs = obs.copy()
            new_obs[step] = _traverse_path(obs[step], path[1:])
            return new_obs
        else:
            raise TypeError(("If observation is a nest, it must be either " +
                             "a dict or namedtuple!"))

    fields = fields or [""]
    for field in fields:
        # remove '' in the path
        path = [step for step in field.split(".") if step]
        observation = _traverse_path(observation, path)
    return observation


@gin.configurable
def reward_clipping(r, minmax=(-1, 1)):
    """
    Clamp immediate rewards to the range [`min`, `max`].

    Can be used as a reward shaping function passed to an algorithm
    (e.g. ActorCriticAlgorithm).
    """
    assert minmax[0] <= minmax[1], "range error"
    return tf.clip_by_value(r, minmax[0], minmax[1])


@gin.configurable
def reward_scaling(r, scale=1):
    """
    Scale immediate rewards by a factor of `scale`.

    Can be used as a reward shaping function passed to an algorithm
    (e.g. ActorCriticAlgorithm).
    """
    return r * scale


def _markdownify_gin_config_str(string, description=''):
    """Convert an gin config string to markdown format.

    Args:
        string (str): the string from gin.operative_config_str()
        description (str): Optional long-form description for this config_str
    Returns:
        The string of the markdown version of the config string.
    """

    # This function is from gin.tf.utils.GinConfigSaverHook
    # TODO: Total hack below. Implement more principled formatting.
    def _process(line):
        """Convert a single line to markdown format."""
        if not line.startswith('#'):
            return '    ' + line

        line = line[2:]
        if line.startswith('===='):
            return ''
        if line.startswith('None'):
            return '    # None.'
        if line.endswith(':'):
            return '#### ' + line
        return line

    output_lines = []

    if description:
        output_lines.append("    # %s\n" % description)

    for line in string.splitlines():
        procd_line = _process(line)
        if procd_line is not None:
            output_lines.append(procd_line)

    return '\n'.join(output_lines)


def get_gin_confg_strs():
    """
    Obtain both the operative and inoperative config strs from gin.

    The operative configuration consists of all parameter values used by
    configurable functions that are actually called during execution of the
    current program, and inoperative configuration consists of all parameter
    configured but not used by configurable functions. See `gin.operative_config_str()`
    and `gin_utils.inoperative_config_str` for more detail on how the config is generated.

    Returns:
        md_operative_config_str (str): a markdown-formatted operative str
        md_inoperative_config_str (str): a markdown-formatted inoperative str
    """
    operative_config_str = gin.operative_config_str()
    md_operative_config_str = _markdownify_gin_config_str(
        operative_config_str,
        'All parameter values used by configurable functions that are actually called'
    )
    md_inoperative_config_str = gin_utils.inoperative_config_str()
    if md_inoperative_config_str:
        md_inoperative_config_str = _markdownify_gin_config_str(
            md_inoperative_config_str,
            "All parameter values configured but not used by program. The configured "
            "functions are either not called or called with explicit parameter values "
            "overriding the config.")
    return md_operative_config_str, md_inoperative_config_str


def summarize_gin_config():
    """Write the operative and inoperative gin config to Tensorboard summary.
    """
    md_operative_config_str, md_inoperative_config_str = get_gin_confg_strs()
    tf.summary.text('gin/operative_config', md_operative_config_str)
    if md_inoperative_config_str:
        tf.summary.text('gin/inoperative_config', md_inoperative_config_str)


def copy_gin_configs(root_dir, gin_files):
    """Copy gin config files to root_dir

    Args:
        root_dir (str): directory path
        gin_files (None|list[str]): list of file paths
    """
    root_dir = os.path.expanduser(root_dir)
    os.makedirs(root_dir, exist_ok=True)
    for f in gin_files:
        shutil.copyfile(f, os.path.join(root_dir, os.path.basename(f)))


def get_gin_file():
    """Get the gin configuration file.

    If FLAGS.gin_file is not set, find gin files under FLAGS.root_dir and
    returns them.
    Returns:
        the gin file(s)
    """
    gin_file = flags.FLAGS.gin_file
    if gin_file is None:
        root_dir = os.path.expanduser(flags.FLAGS.root_dir)
        gin_file = glob.glob(os.path.join(root_dir, "*.gin"))
        assert gin_file, "No gin files are found! Please provide"
    return gin_file


def tensor_extend(x, y):
    """Extending tensor with new_slice.

    new_slice.shape should be same as tensor.shape[1:]
    Args:
        x (Tensor): tensor to be extended
        y (Tensor): the tensor which will be appended to `x`
    Returns:
        the extended tensor. Its shape is (x.shape[0]+1, x.shape[1:])
    """
    return tf.concat([x, tf.expand_dims(y, axis=0)], axis=0)


def tensor_extend_zero(x):
    """Extending tensor with zeros.

    new_slice.shape should be same as tensor.shape[1:]
    Args:
        x (Tensor): tensor to be extended
    Returns:
        the extended tensor. Its shape is (x.shape[0]+1, x.shape[1:])
    """
    return tf.concat(
        [x,
         tf.expand_dims(tf.zeros(tf.shape(x)[1:], dtype=x.dtype), axis=0)],
        axis=0)


def explained_variance(ypred, y):
    """Computes fraction of variance that ypred explains about y.

    Adapted from baselines.ppo2 explained_variance()

    Interpretation:
        ev=0  =>  might as well have predicted zero
        ev=1  =>  perfect prediction
        ev<0  =>  worse than just predicting zero

    Args:
        ypred (Tensor): prediction for y
        y (Tensor): target
    Returns:
        1 - Var[y-ypred] / Var[y]
    """
    ypred = tf.reshape(ypred, [-1])
    y = tf.reshape(y, [-1])
    _, vary = tf.nn.moments(y, axes=(0, ))
    return 1 - tf.nn.moments(y - ypred, axes=(0, ))[1] / (vary + 1e-30)


def sample_action_distribution(actions_or_distributions, seed=None):
    """Sample actions from distributions
    Args:
        actions_or_distributions (nested tf.Tensor|nested Distribution]):
            tf.Tensor represent parameter `loc` for tfp.distributions.Deterministic
            and others for tfp.distributions.Distribution instances
        seed (Any):Any Python object convertible to string, supplying the
            initial entropy.  If `None`, operations seeded with seeds
            drawn from this `SeedStream` will follow TensorFlow semantics
            for not being seeded.
    Returns:
        sampled actions
    """

    distributions = tf.nest.map_structure(to_distribution,
                                          actions_or_distributions)
    seed_stream = tfp.distributions.SeedStream(seed=seed, salt='sample')
    return tf.nest.map_structure(lambda d: d.sample(seed=seed_stream()),
                                 distributions)


def to_distribution_spec(spec):
    """Convert spec to to DistributionSpec.

    Args:
        spec (tf.TensorSpec | DistributionSpec): spec
    """
    if isinstance(spec, tf.TensorSpec):
        return DistributionSpec(
            tfp.distributions.Deterministic,
            input_params_spec={"loc": spec},
            sample_spec=spec)
    return spec


def to_distribution(action_or_distribution):
    """Convert action_or_distribution to to Distribution.

    Args:
        action_or_distribution (nested tf.Tensor|nested Distribution]):
            tf.Tensor represent parameter `loc` for tfp.distributions.Deterministic
            and others for tfp.distributions.Distribution instance
    Returns:
        nested Distribution
    """

    def _to_dist(action_or_distribution):
        if isinstance(action_or_distribution, tf.Tensor):
            return tfp.distributions.Deterministic(loc=action_or_distribution)
        else:
            return action_or_distribution

    return tf.nest.map_structure(_to_dist, action_or_distribution)


def get_initial_policy_state(batch_size, policy_state_spec):
    """
    Return zero tensors as the initial policy states.
    Args:
        batch_size (int): number of policy states created
        policy_state_spec (nested structure): each item is a tensor spec for
            a state

    Returns:
        state (nested structure): each item is a tensor with the first dim equal
            to `batch_size`. The remaining dims are consistent with
            the corresponding state spec of `policy_state_spec`.
    """
    return zero_tensor_from_nested_spec(policy_state_spec, batch_size)


def get_initial_time_step(env):
    """
    Return the initial time step
    Args:
        env (TFPyEnvironment):

    Returns:
        time_step (ActionTimeStep): the init time step with actions as zero
            tensors
    """
    time_step = env.current_time_step()
    action = zero_tensor_from_nested_spec(env.action_spec(), env.batch_size)
    return make_action_time_step(time_step, action)


def algorithm_step(algorithm_step_func, time_step, state):
    """
    Perform an algorithm step on a time step.
    Always convert the output `policy_step.action` to an action distribution

    Args:
        algorithm_step_func (Callable): step function from algorithm. Can be
            algorithm.predict, algorithm.rollout or algorithm.train_step
        time_step (ActionTimeStep):
        state (tf.nest): could be consistent with either
            `algorithm.train_state_spec` or `algorithm.predict_state_spec`

    Returns:
        policy_step (PolicyStep): policy step should always have action
            distributions, even for deterministic ones
    """
    policy_step = algorithm_step_func(time_step, state)
    return policy_step._replace(action=to_distribution(policy_step.action))


def transpose2(x, dim1, dim2):
    """Transpose two axes `dim1` and `dim2` of a tensor."""
    perm = list(range(len(x.shape)))
    perm[dim1] = dim2
    perm[dim2] = dim1
    return tf.transpose(x, perm)


def sample_policy_action(policy_step):
    """Sample an action for a policy step and replace the old distribution."""
    action = sample_action_distribution(policy_step.action)
    policy_step = policy_step._replace(action=action)
    return policy_step


def flatten_once(t):
    """Flatten a tensor along axis=0 and axis=1."""
    return tf.reshape(t, [-1] + list(t.shape[2:]))


_env = None


def set_global_env(env):
    """Set global env."""
    global _env
    _env = env


@gin.configurable
def get_observation_spec():
    """Get the `TensorSpec` of observations provided by the global environment.

    This spec is used for creating models only! All uint8 dtype will be converted
    to tf.float32 as a temporary solution, to be consistent with
    `image_scale_transformer()`. See

    https://github.com/HorizonRobotics/alf/pull/239#issuecomment-544644558

    Returns:
        A `TensorSpec`, or a nested dict, list or tuple of
        `TensorSpec` objects, which describe the observation.
    """
    assert _env, "set a global env by `set_global_env` before using the function"
    specs = _env.observation_spec()
    return tf.nest.map_structure(
        lambda spec: (tf.TensorSpec(spec.shape, tf.float32)
                      if spec.dtype == tf.uint8 else spec), specs)


@gin.configurable
def get_states_shape():
    """Get the tensor shape of internal states of the agent provided by
      the global environment

    Returns:
        list of ints.
        Returns 0 if internal states is not part of observation.
    """
    assert _env, "set a global env by `set_global_env` before using the function"
    assert isinstance(_env.observation_spec(), dict), "observation not a dict"
    if 'states' in _env.observation_spec():
        return _env.observation_spec()['states'].shape
    else:
        return 0


@gin.configurable
def get_action_spec():
    """Get the specs of the Tensors expected by `step(action)` of the global environment.

    Returns:
      An single `TensorSpec`, or a nested dict, list or tuple of
      `TensorSpec` objects, which describe the shape and
      dtype of each Tensor expected by `step()`.
    """
    assert _env, "set a global env by `set_global_env` before using the function"
    return _env.action_spec()


@gin.configurable
def get_vocab_size():
    """Get the vocabulary size of observations provided by the global environment.

    Returns:
        vocab_size (int): size of the environment's/teacher's vocabulary.
        Returns 0 if language is not part of observation.
    """
    assert _env, "set a global env by `set_global_env` before using the function"
    if 'sentence' in _env.observation_spec():
        return _env.observation_spec()['sentence'].shape[0]
    else:
        return 0


def extract_spec(nest, from_dim=1):
    """
    Extract tensor spec for each element of a nested structure.
    It assumes that the first dimension of each element is the batch size.

    Args:
        from_dim (int): ignore dimension before this when constructing the spec.
        nest (nested structure): each leaf node of the nested structure is a
            tensor of the same batch size
    Returns:
        spec (nested structure): each leaf node of the returned nested spec is the
            corresponding spec (excluding batch size) of the element of `nest`
    """
    return tf.nest.map_structure(
        lambda t: tf.TensorSpec(t.shape[from_dim:], t.dtype), nest)


@gin.configurable
def active_action_target_entropy(active_action_portion=0.2, min_entropy=0.3):
    """Automatically compute target entropy given the action spec. Currently
    support discrete actions only.

    The general idea is that we assume N*k actions having uniform probs for a good
    policy. Thus the target entropy should be log(N*k), where N is the total
    number of discrete actions and k is the active action portion.

    TODO: incorporate this function into EntropyTargetAlgorithm if it proves
    to be effective.

    Args:
        active_action_portion (float): a number in (0, 1]. Ideally, this value
            should be greater than `1/num_actions`. If it's not, it will be
            ignored.
        min_entropy (float): the minimum possible entropy. If the auto-computed
            entropy is smaller than this value, then it will be replaced.

    Returns:
        target_entropy (float): the target entropy for EntropyTargetAlgorithm
    """
    assert active_action_portion <= 1.0 and active_action_portion > 0
    action_spec = get_action_spec()
    assert tensor_spec.is_discrete(
        action_spec), "only support discrete actions!"
    num_actions = action_spec.maximum - action_spec.minimum + 1
    return max(math.log(num_actions * active_action_portion), min_entropy)


def write_gin_configs(root_dir, gin_file):
    """
    Write a gin configration to a file. Because the user can
    1) manually change the gin confs after loading a conf file into the code, or
    2) include a gin file in another gin file while only the latter might be
       copied to `root_dir`.
    So here we just dump the actual used gin conf string to a file.

    Args:
        root_dir (str): directory path
        gin_file (str): a single file path for storing the gin configs. Only
            the basename of the path will be used.
    """
    root_dir = os.path.expanduser(root_dir)
    os.makedirs(root_dir, exist_ok=True)
    file = os.path.join(root_dir, os.path.basename(gin_file))

    md_operative_config_str, md_inoperative_config_str = get_gin_confg_strs()
    config_str = md_operative_config_str + '\n\n' + md_inoperative_config_str

    # the mark-down string can just be safely written as a python file
    with open(file, "w") as f:
        f.write(config_str)


def warning_once(msg, *args):
    """Generate warning message once

    Args:
        msg: str, the message to be logged.
        *args: The args to be substitued into the msg.
    """
    logging.log_every_n(logging.WARNING, msg, 1 << 62, *args)


def create_tensor_array(spec, num_steps, batch_size, clear_after_read=None):
    """Create nested TensorArray based spec.

    Args:
        spec (nested TensorSpec): spec for each step (without batch dimension)
        num_steps (int): size (length) of the TensorArray to be created
        batch_size (int): batch size of each element
        clear_after_read (bool): If True, clear TensorArray values after reading
            them. This disables read-many semantics, but allows early release of
            memory.
    Returns:
        nested TensorArray with the same structure as spec

    """

    def _create_ta(s):
        return tf.TensorArray(
            dtype=s.dtype,
            size=num_steps,
            clear_after_read=clear_after_read,
            element_shape=tf.TensorShape([batch_size]).concatenate(s.shape))

    return tf.nest.map_structure(_create_ta, spec)


def create_and_unstack_tensor_array(tensors, clear_after_read=True):
    """Create tensor array from nested tensors.

    Args:
        tensors (nestd Tensor): nested Tensors
        clear_after_read (bool): If True, clear TensorArray values after reading
            them. This disables read-many semantics, but allows early release of
            memory.
    Returns:
        nested TensorArray with the same structure as tensors
    """
    flattened = tf.nest.flatten(tensors)
    if len(flattened) == 0:
        return tf.nest.map_structure(lambda a: a, tensors)
    spec = extract_spec(tensors, from_dim=2)
    # element_shape of TensorArray must be explicit shape (i.e., known)
    batch_size = flattened[0].shape[1]
    # size of TensorArray cannot be None, though it can be a Tensor
    num_steps = tf.shape(flattened[0])[0]
    ta = create_tensor_array(
        spec, num_steps, batch_size, clear_after_read=clear_after_read)
    ta = tf.nest.map_structure(lambda elem, ta: ta.unstack(elem), tensors, ta)
    return ta
