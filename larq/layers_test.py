import inspect

import numpy as np
import pytest
import tensorflow as tf

import larq as lq
from larq import testing_utils

PARAMS_ALL_LAYERS = [
    (lq.layers.QuantDense, tf.keras.layers.Dense, (3, 2), dict(units=3)),
    (
        lq.layers.QuantConv1D,
        tf.keras.layers.Conv1D,
        (2, 3, 7),
        dict(filters=2, kernel_size=3),
    ),
    (
        lq.layers.QuantConv2D,
        tf.keras.layers.Conv2D,
        (2, 3, 7, 6),
        dict(filters=2, kernel_size=3),
    ),
    (
        lq.layers.QuantConv3D,
        tf.keras.layers.Conv3D,
        (2, 3, 7, 6, 5),
        dict(filters=2, kernel_size=3),
    ),
    (
        lq.layers.QuantConv2DTranspose,
        tf.keras.layers.Conv2DTranspose,
        (2, 3, 7, 6),
        dict(filters=2, kernel_size=3),
    ),
    (
        lq.layers.QuantConv3DTranspose,
        tf.keras.layers.Conv3DTranspose,
        (2, 3, 7, 6, 5),
        dict(filters=2, kernel_size=3),
    ),
    (
        lq.layers.QuantLocallyConnected1D,
        tf.keras.layers.LocallyConnected1D,
        (2, 8, 5),
        dict(filters=4, kernel_size=3),
    ),
    (
        lq.layers.QuantLocallyConnected2D,
        tf.keras.layers.LocallyConnected2D,
        (8, 6, 10, 4),
        dict(filters=3, kernel_size=3),
    ),
]

PARAMS_SEP_LAYERS = [
    (lq.layers.QuantSeparableConv1D, tf.keras.layers.SeparableConv1D, (2, 3, 7),),
    (lq.layers.QuantSeparableConv2D, tf.keras.layers.SeparableConv2D, (2, 3, 7, 6),),
]


class TestLayers:
    @pytest.mark.parametrize(
        "quantized_layer, layer, input_shape, kwargs", PARAMS_ALL_LAYERS
    )
    def test_binarization(
        self, quantized_layer, layer, input_shape, kwargs, keras_should_run_eagerly
    ):
        input_data = testing_utils.random_input(input_shape)
        random_weight = np.random.random() - 0.5

        with lq.metrics.scope(["flip_ratio"]):
            quant_output = testing_utils.layer_test(
                quantized_layer,
                kwargs=dict(
                    **kwargs,
                    kernel_quantizer="ste_sign",
                    input_quantizer="ste_sign",
                    kernel_initializer=tf.keras.initializers.constant(random_weight),
                ),
                input_data=input_data,
                should_run_eagerly=keras_should_run_eagerly,
            )

        fp_model = tf.keras.models.Sequential(
            [
                layer(
                    **kwargs,
                    kernel_initializer=tf.keras.initializers.constant(
                        np.sign(random_weight)
                    ),
                    input_shape=input_shape[1:],
                )
            ]
        )

        np.testing.assert_allclose(quant_output, fp_model.predict(np.sign(input_data)))

    @pytest.mark.parametrize("quantized_layer, layer, input_shape", PARAMS_SEP_LAYERS)
    def test_separable_layers(
        self, quantized_layer, layer, input_shape, keras_should_run_eagerly
    ):
        input_data = testing_utils.random_input(input_shape)
        random_d_kernel = np.random.random() - 0.5
        random_p_kernel = np.random.random() - 0.5

        with lq.metrics.scope(["flip_ratio"]):
            quant_output = testing_utils.layer_test(
                quantized_layer,
                kwargs=dict(
                    filters=3,
                    kernel_size=3,
                    depthwise_quantizer="ste_sign",
                    pointwise_quantizer="ste_sign",
                    input_quantizer="ste_sign",
                    depthwise_initializer=tf.keras.initializers.constant(
                        random_d_kernel
                    ),
                    pointwise_initializer=tf.keras.initializers.constant(
                        random_p_kernel
                    ),
                ),
                input_data=input_data,
                should_run_eagerly=keras_should_run_eagerly,
            )

        fp_model = tf.keras.models.Sequential(
            [
                layer(
                    filters=3,
                    kernel_size=3,
                    depthwise_initializer=tf.keras.initializers.constant(
                        np.sign(random_d_kernel)
                    ),
                    pointwise_initializer=tf.keras.initializers.constant(
                        np.sign(random_p_kernel)
                    ),
                    input_shape=input_shape[1:],
                )
            ]
        )

        np.testing.assert_allclose(quant_output, fp_model.predict(np.sign(input_data)))

    def test_depthwise_layers(self, keras_should_run_eagerly):
        input_data = testing_utils.random_input((2, 3, 7, 6))
        random_weight = np.random.random() - 0.5

        with lq.metrics.scope(["flip_ratio"]):
            quant_output = testing_utils.layer_test(
                lq.layers.QuantDepthwiseConv2D,
                kwargs=dict(
                    kernel_size=3,
                    depthwise_quantizer="ste_sign",
                    input_quantizer="ste_sign",
                    depthwise_initializer=tf.keras.initializers.constant(random_weight),
                ),
                input_data=input_data,
                should_run_eagerly=keras_should_run_eagerly,
            )

        fp_model = tf.keras.models.Sequential(
            [
                tf.keras.layers.DepthwiseConv2D(
                    kernel_size=3,
                    depthwise_initializer=tf.keras.initializers.constant(
                        np.sign(random_weight)
                    ),
                    input_shape=input_data.shape[1:],
                )
            ]
        )

        np.testing.assert_allclose(quant_output, fp_model.predict(np.sign(input_data)))


class TestLayerWarns:
    def test_layer_warns(self, caplog):
        lq.layers.QuantDense(5, kernel_quantizer="ste_sign")
        assert len(caplog.records) >= 1
        assert "kernel_constraint" in caplog.text

    def test_layer_does_not_warn(self, caplog):
        lq.layers.QuantDense(
            5, kernel_quantizer="ste_sign", kernel_constraint="weight_clip"
        )
        assert caplog.records == []

    def test_depthwise_layer_warns(self, caplog):
        lq.layers.QuantDepthwiseConv2D(5, depthwise_quantizer="ste_sign")
        assert len(caplog.records) >= 1
        assert "depthwise_constraint" in caplog.text

    def test_depthwise_layer_does_not_warn(self, caplog):
        lq.layers.QuantDepthwiseConv2D(
            5, depthwise_quantizer="ste_sign", depthwise_constraint="weight_clip"
        )
        assert caplog.records == []

    def test_separable_layer_warns(self, caplog):
        lq.layers.QuantSeparableConv2D(
            3, 3, depthwise_quantizer="ste_sign", pointwise_quantizer="ste_sign"
        )
        assert "depthwise_constraint" in caplog.text
        assert "pointwise_constraint" in caplog.text

    def test_separable_layer_does_not_warn(self, caplog):
        lq.layers.QuantSeparableConv2D(
            3,
            3,
            depthwise_quantizer="ste_sign",
            pointwise_quantizer="ste_sign",
            depthwise_constraint="weight_clip",
            pointwise_constraint="weight_clip",
        )
        assert caplog.records == []


def test_metrics():
    model = tf.keras.models.Sequential(
        [lq.layers.QuantDense(3, kernel_quantizer="ste_sign", input_shape=(32,))]
    )
    model.compile(loss="mse", optimizer="sgd")
    assert len(model.layers[0]._metrics) == 0

    with lq.metrics.scope(["flip_ratio"]):
        model = tf.keras.models.Sequential(
            [lq.layers.QuantDense(3, kernel_quantizer="ste_sign", input_shape=(32,))]
        )
    model.compile(loss="mse", optimizer="sgd")
    assert len(model.layers[0]._metrics) == 1

    model = tf.keras.models.Sequential(
        [
            lq.layers.QuantDense(
                3,
                kernel_quantizer="ste_sign",
                metrics=["flip_ratio"],
                input_shape=(32,),
            )
        ]
    )
    model.compile(loss="mse", optimizer="sgd")
    assert len(model.layers[0]._metrics) == 1


@pytest.mark.parametrize(
    "quant_layer,layer",
    [
        (lq.layers.QuantDense, tf.keras.layers.Dense),
        (lq.layers.QuantConv1D, tf.keras.layers.Conv1D),
        (lq.layers.QuantConv2D, tf.keras.layers.Conv2D),
        (lq.layers.QuantConv3D, tf.keras.layers.Conv3D),
        (lq.layers.QuantConv2DTranspose, tf.keras.layers.Conv2DTranspose),
        (lq.layers.QuantConv3DTranspose, tf.keras.layers.Conv3DTranspose),
        (lq.layers.QuantLocallyConnected1D, tf.keras.layers.LocallyConnected1D),
        (lq.layers.QuantLocallyConnected2D, tf.keras.layers.LocallyConnected2D),
        (lq.layers.QuantDepthwiseConv2D, tf.keras.layers.DepthwiseConv2D),
    ],
)
def test_layer_kwargs(quant_layer, layer):
    quant_params = inspect.signature(quant_layer).parameters
    params = inspect.signature(layer).parameters

    quant_params_list = list(quant_params.keys())
    params_list = list(params.keys())

    for p in (
        "input_quantizer",
        "kernel_quantizer",
        "depthwise_quantizer",
        "pointwise_quantizer",
        "metrics",
    ):
        try:
            quant_params_list.remove(p)
        except ValueError:
            pass
    assert quant_params_list == params_list

    for param in params_list:
        assert quant_params.get(param).default == params.get(param).default  # type: ignore
