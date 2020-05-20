import io
import sys
import os
import torch

from retinanet.model import Model
from retinanet.data import DataIterator

from timeit import default_timer as timer
from onnx import numpy_helper
import onnxruntime
import numpy as np
import onnx


iterations = 10
data_dir = 'test_data_set_0'


def flatten(inputs):
    return [[flatten(i) for i in inputs] if isinstance(inputs, (list, tuple)) else inputs]


def update_flatten_list(inputs, res_list):
    for i in inputs:
        res_list.append(i) if not isinstance(i, (list, tuple)) else update_flatten_list(i, res_list)
    return res_list


def to_numpy(x):
    if type(x) is not np.ndarray:
        x = x.detach().cpu().numpy() if x.requires_grad else x.cpu().numpy()
    return x


def save_tensor_proto(file_path, name, data):
    tp = numpy_helper.from_array(data)
    tp.name = name

    with open(file_path, 'wb') as f:
        f.write(tp.SerializeToString())


def save_data(test_data_dir, prefix, names, data_list):
    if isinstance(data_list, torch.autograd.Variable) or isinstance(data_list, torch.Tensor):
        data_list = [data_list]
    for i, d in enumerate(data_list):
        d = d.data.cpu().numpy()
        save_tensor_proto(os.path.join(test_data_dir, '{0}_{1}.pb'.format(prefix, i)), names[i], d)


def save_model(name, model, inputs, outputs, input_names=None, output_names=None, **kwargs):
    if hasattr(model, 'train'):
        model.train(False)
    dir = './'
    if not os.path.exists(dir):
        os.makedirs(dir)
    dir = os.path.join(dir, 'test_' + name)
    if not os.path.exists(dir):
        os.makedirs(dir)

    inputs_flatten = flatten(inputs)
    inputs_flatten = update_flatten_list(inputs_flatten, [])
    outputs_flatten = flatten(outputs)
    outputs_flatten = update_flatten_list(outputs_flatten, [])
    if input_names is None:
        input_names = []
        for i, _ in enumerate(inputs_flatten):
            input_names.append('input' + str(i+1))
    else:
        np.testing.assert_equal(len(input_names), len(inputs_flatten),
                                "Number of input names provided is not equal to the number of inputs.")

    if output_names is None:
        output_names = []
        for i, _ in enumerate(outputs_flatten):
            output_names.append('output' + str(i+1))
    else:
        np.testing.assert_equal(len(output_names), len(outputs_flatten),
                                "Number of output names provided is not equal to the number of output.")

    model_dir = os.path.join(dir, 'model.onnx')
    torch.onnx.export(model, inputs, model_dir, verbose=True, input_names=input_names,
                      output_names=output_names, example_outputs=outputs, **kwargs)

    test_data_dir = os.path.join(dir, data_dir)
    if not os.path.exists(test_data_dir):
        os.makedirs(test_data_dir)

    save_data(test_data_dir, "input", input_names, inputs_flatten)
    save_data(test_data_dir, "output", output_names, outputs_flatten)

    return model_dir, test_data_dir


def inference(file, inputs, outputs):
    inputs_flatten = flatten(inputs)
    inputs_flatten = update_flatten_list(inputs_flatten, [])
    outputs_flatten = flatten(outputs)
    outputs_flatten = update_flatten_list(outputs_flatten, [])

    sess = onnxruntime.InferenceSession(file)
    ort_inputs = dict((sess.get_inputs()[i].name, to_numpy(input)) for i, input in enumerate(inputs_flatten))
    res = sess.run(None, ort_inputs)

    if outputs is not None:
        print("== Checking model output ==")
        [np.testing.assert_allclose(to_numpy(output), res[i], rtol=1e-03, atol=1e-05) for i, output in enumerate(outputs_flatten)]
        print("== Done ==")


def perf_run(sess, feeds, min_counts=5, min_duration_seconds=10):
    # warm up
    sess.run([], feeds)

    start = timer()
    run = True
    count = 0
    per_iter_cost = []
    while run:
        iter_start = timer()
        sess.run([], feeds)
        end = timer()
        count = count + 1
        per_iter_cost.append(end - iter_start)
        if end - start >= min_duration_seconds and count >= min_counts:
            run = False
    return count, (end - start), per_iter_cost


def torch_inference(model, input):
    print("====== Torch Inference ======")
    output=model(input)
    with torch.no_grad():
        total = []
        for x in range(iterations):
            t0 = timer()
            output_1 = model(input)
            iter_time = timer() - t0
            total.append(iter_time)
            duration = sum(total) * 1000 / iterations

    print("run for {} iterations, avg {} ms".format(iterations, duration))
    return output


def ort_inference(file, inputs_flatten, outputs_flatten):
    print("====== ORT Inference ======")
    ort_sess = onnxruntime.InferenceSession(file)
    ort_inputs = dict((ort_sess.get_inputs()[i].name, to_numpy(input)) for i, input in enumerate(inputs_flatten))
    ort_outs = ort_sess.run(None, ort_inputs)
    if outputs_flatten is not None:
        print("== Checking model output ==")
        [np.testing.assert_allclose(to_numpy(output), ort_outs[i], rtol=1e-03, atol=1e-05) for i, output in
         enumerate(outputs_flatten)]

    count, duration, per_iter_cost = perf_run(ort_sess, ort_inputs, min_counts=iterations)
    avg_rnn = sum(per_iter_cost) * 1000 / count
    print('run for {} iterations, avg {} ms'.format(count, avg_rnn))
    print("== Done ==")


def get_image_from_url(url, size=None):
    import requests
    from PIL import Image
    from io import BytesIO
    from torchvision import transforms

    data = requests.get(url)
    image = Image.open(BytesIO(data.content)).convert("RGB")
    image = image.resize(size, Image.BILINEAR)

    to_tensor = transforms.ToTensor()
    return to_tensor(image)


def get_test_images():
    image_url = "http://farm3.staticflickr.com/2469/3915380994_2e611b1779_z.jpg"
    image = get_image_from_url(url=image_url, size=(384, 384))
    images = torch.unsqueeze(image, dim=0)
    return images


# Download pretrained model from:
# https://github.com/NVIDIA/retinanet-examples/releases/tag/19.04
model, state = Model.load('retinanet_rn101fpn/retinanet_rn101fpn.pth')
model.eval()
model.exporting = True
image = get_test_images()
output = torch_inference(model, image)

# Test exported model with TensorProto data saved in files
inputs_flatten = flatten(image.detach().cpu().numpy())
inputs_flatten = update_flatten_list(inputs_flatten, [])
outputs_flatten = flatten(output)
outputs_flatten = update_flatten_list(outputs_flatten, [])

model_dir, data_dir = save_model('retinanet_resnet101', model.cpu(), image, output, input_names=['input'],
                                 opset_version=9)

ort_inference(model_dir, inputs_flatten, outputs_flatten)

