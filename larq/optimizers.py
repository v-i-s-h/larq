"""Neural networks with extremely low-precision weights and activations, such as
Binarized Neural Networks (BNNs), usually contain a mix of low-precision weights (e.g.
1-bit) and  higher-precision weights (e.g. 8-bit, 16-bit, or 32-bit). Examples of this
include the first and last layers of image classificiation models, which have
higher-precision weights in most BNN architectures from the literature.

Training a BNN, then, consists of optimizing both low-precision and higher-precision
weights. In `larq`, we provide a mechanism to target different bit-precision variables
with different optimizers using the `CaseOptimizer` class. Modeled after the
[`tf.case`](https://www.tensorflow.org/api_docs/python/tf/case) signature,
`CaseOptimizer` accepts pairs of predicates and optimizers. A predicate, given a
variable, decides whether its optimizer should train that variable.

A `CaseOptimizer` behaves much like any other
[Keras optimizer](https://www.tensorflow.org/api_docs/python/tf/keras/optimizers), and
once you instantiate it you can pass it to your `model.compile()` as usual. To
instantiate a `CaseOptimzer`, pass one or a list of `(predicate, optimizer)` tuples,
along with a `default` optimizer which trains any variables not claimed by another
optimizer. A variable may not be claimed by more than one optimizer's predicate.

!!! example
    ```python
    case_optimizer = lq.optimizers.CaseOptimizer(
        (
            lq.optimizers.Bop.is_binary_variable,  # predicate
            lq.optimizers.Bop(threshold=1e-6, gamma=1e-3),  # optimizer
        ),
        default_optimizer=tf.keras.optimizers.Adam(0.01),
    )
    ```
"""


import warnings
from copy import deepcopy

import tensorflow as tf

import larq as lq
from larq import utils

__all__ = ["Bop", "CaseOptimizer"]


@utils.register_keras_custom_object
class CaseOptimizer(tf.keras.optimizers.Optimizer):
    """An optmizer wrapper that applies different optimizers to a subset of variables.

    An optimizer is used to train a variable iff its accompanying predicate evaluates to
    `True`.

    For each variable, at most one optimizer's predicate may evaluate to `True`. If no
    optimizer's predicate evaluates to `True` for a variable, it is trained with the
    `default_optimizer`. If a variable is claimed by no optimizers and
    `default_optimizer == None`, the variable is not trained.

    # Arguments
    predicate_optimizer_pairs: One or more `(pred, tf.keras.optimizers.Optimzer)` pairs,
        where `pred`  takes one `tf.Variable` as argument and returns `True` if the
        optimizer should be used for that variable, e.g. `pred(var) == True`.
    default_optimizer: A `tf.keras.optimizers.Optimizer` to be applied to any variable
        not claimed by any other optimizer. (Must be passed as keyword argument.)
    """

    def __init__(
        self, *predicate_optimizer_pairs, default_optimizer=None, name="optimizer_case"
    ):
        super().__init__(name=name)

        # Type checks for (predicate, optimizer) pairs
        for i, (predicate, optimizer) in enumerate(predicate_optimizer_pairs):
            if not callable(predicate):
                raise TypeError(
                    f"Expected callable predicate at `predicate_optimizer_pairs[{i}][0]` but got `{type(predicate)}`."
                )
            if not isinstance(optimizer, tf.keras.optimizers.Optimizer):
                raise TypeError(
                    f"Expected `tf.keras.optimizers.Optimizer` at `predicate_optimizer_pairs[{i}][1]` but got `{type(optimizer)}`."
                )

        # Type check for default optimizers
        if default_optimizer is not None and not isinstance(
            default_optimizer, tf.keras.optimizers.Optimizer
        ):
            raise TypeError(
                f"Expected `tf.keras.optimizers.Optimizer` for `default_optimizer` but got `{type(default_optimizer)}`."
            )

        self.pred_opt_pairs = predicate_optimizer_pairs
        self.default = default_optimizer

        self.var_opt_mapping = None

        # List of optimizers ending in `default_optimizer`, for easier internal access
        self.optimizers = [opt for (_, opt) in self.pred_opt_pairs]

        if self.default:
            self.optimizers.append(self.default)
            self.DEFAULT_OPT_INDEX = len(self.pred_opt_pairs)

    def apply_gradients(self, grads_and_vars, name=None):
        """Apply gradients to variables for each optimizer.

        On the first call to `apply_gradients()`, compute the mapping from variables to
        optimizers and cache it in the `self.var_opt_mapping` dict for serialization and
        faster access.
        """

        if self.var_opt_mapping is None:
            # Convert `grads_and_vars` to list so we can iterate multiple times over it
            grads_and_vars = list(grads_and_vars)
            self._compute_var_opt_mapping(grads_and_vars)

        # Split gradients and variables into a separate list for each optimizer
        grad_var_lists = [[] for _ in range(len(self.pred_opt_pairs) + 1)]
        for grad, var in grads_and_vars:
            if var.name in self.var_opt_mapping:
                grad_var_lists[self.var_opt_mapping[var.name]].append((grad, var))

        # Apply gradients to each optimizer
        train_ops = [
            optimizer.apply_gradients(opt_grads_and_vars)
            for optimizer, opt_grads_and_vars in zip(self.optimizers, grad_var_lists)
        ]

        return tf.group(*train_ops, name="train_with_group")

    def get_config(self):
        optimizer_configs = [opt.get_config() for (_, opt) in self.pred_opt_pairs]
        default_config = self.default.get_config()

        config = {
            "optimizer_configs": [
                {"class_name": optimizer_config["name"], "config": optimizer_config}
                for optimizer_config in optimizer_configs
            ],
            "default_config": {
                "class_name": default_config["name"],
                "config": default_config,
            },
            "var_opt_mapping": self.var_opt_mapping,  # serialized instead of `pred`s
        }
        return {**super().get_config(), **config}

    @classmethod
    def from_config(cls, original_config, custom_objects=None):
        config = deepcopy(original_config)

        case_optimizer = cls(
            *[  # `(pred, opt)` tuples
                (
                    lambda _: False,  # placeholder callable (`pred` is not serialized)
                    tf.keras.optimizers.deserialize(  # optimizer `opt`
                        opt_config, custom_objects=custom_objects
                    ),
                )
                for opt_config in config["optimizer_configs"]
            ],
            default_optimizer=tf.keras.optimizers.deserialize(
                config["default_config"], custom_objects=custom_objects
            ),
        )

        # Since we no longer have the `pred`s, we set the mapping explicitly
        case_optimizer.var_opt_mapping = config["var_opt_mapping"]

        return case_optimizer

    def _compute_var_opt_mapping(self, grads_and_vars):
        """Compute a unique mapping from variables to optimizer indices."""

        self.var_opt_mapping = {}

        for grad, var in grads_and_vars:
            num_optimizers = 0

            # Find the optimizer(s) that want to claim this variable
            for optimizer_index, (predicate, _) in enumerate(self.pred_opt_pairs):
                if predicate(var):
                    self.var_opt_mapping[var.name] = optimizer_index
                    num_optimizers += 1

            if num_optimizers > 1:
                raise ValueError(f"Variable `{var}` claimed by multiple optimizers.")
            if num_optimizers == 0:
                if self.default is not None:
                    self.var_opt_mapping[var.name] = self.DEFAULT_OPT_INDEX
                else:
                    warnings.warn(
                        f"No `default_optimizer` provided to train variable `{var}`."
                    )


@utils.register_keras_custom_object
class Bop(tf.keras.optimizers.Optimizer):
    """Binary optimizer (Bop).

    Bop is a latent-free optimizer for Binarized Neural Networks (BNNs) and
    Binary Weight Networks (BWN).

    Bop maintains an exponential moving average of the gradients controlled by
    `gamma`. If this average exceeds the `threshold`, a weight is flipped.
    Additionally, Bop accepts a regular optimizer that is applied to the
    non-binary weights in the network.

    The hyperparameter `gamma` is somewhat analogues to the learning rate in
    SGD methods: a high `gamma` results in rapid convergence but also makes
    training more noisy.

    Note that the default `threshold` is not optimal for all situations.
    Setting the threshold too high results in little learning, while setting it
    too low results in overly noisy behaviour.

    !!! example
        ```python
        optimizer = lq.optimizers.CaseOptimizer(
            (
                lq.optimizers.Bop.is_binary_variable,
                lq.optimizers.Bop(),
            ),
            default_optimizer=tf.keras.optimizers.Adam(0.01),  # for FP weights
        )
        ```

    # Arguments
    threshold: determines to whether to flip each weight.
    gamma: the adaptivity rate.
    name: name of the optimizer.

    # References
    - [Latent Weights Do Not Exist: Rethinking Binarized Neural Network Optimization](https://arxiv.org/abs/1906.02107)
    """

    def __init__(self, threshold=1e-7, gamma=1e-2, name="Bop", **kwargs):
        super().__init__(name=name, **kwargs)

        self._set_hyper("threshold", threshold)
        self._set_hyper("gamma", gamma)

    def _create_slots(self, var_list):
        for var in var_list:
            self.add_slot(var, "m")

    def _get_decayed_hyper(self, name, var_dtype):
        hyper = self._get_hyper(name, var_dtype)
        if isinstance(hyper, tf.keras.optimizers.schedules.LearningRateSchedule):
            local_step = tf.cast(self.iterations, var_dtype)
            hyper = tf.cast(hyper(local_step), var_dtype)
        return hyper

    def _resource_apply_dense(self, grad, var):
        var_dtype = var.dtype.base_dtype
        gamma = self._get_decayed_hyper("gamma", var_dtype)
        threshold = self._get_decayed_hyper("threshold", var_dtype)
        m = self.get_slot(var, "m")

        m_t = tf.compat.v1.assign(
            m, (1 - gamma) * m + gamma * grad, use_locking=self._use_locking
        )
        var_t = lq.math.sign(-tf.sign(var * m_t - threshold) * var)
        return tf.compat.v1.assign(var, var_t, use_locking=self._use_locking).op

    def _resource_apply_sparse(self, grad, var, indices):
        raise NotImplementedError()

    def get_config(self):
        config = {
            "threshold": self._serialize_hyperparameter("threshold"),
            "gamma": self._serialize_hyperparameter("gamma"),
        }
        return {**super().get_config(), **config}

    @classmethod
    def from_config(cls, config, custom_objects=None):
        for hyper in ("gamma", "threshold"):
            if hyper in config and isinstance(config[hyper], dict):
                config[hyper] = tf.keras.optimizers.schedules.deserialize(
                    config[hyper], custom_objects=custom_objects
                )
        return cls(**config)

    @staticmethod
    def is_binary_variable(var):
        """Returns True for binary variables named using the Larq Zoo naming scheme.

        This is an example of a predictate that can be used by the `CaseOptimizer`.

        # Arguments
        var: a `tf.Variable`.
        """
        return "/kernel" in var.name and "quant_" in var.name
