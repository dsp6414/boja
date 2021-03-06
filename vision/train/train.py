# Based on sample code from the TorchVision 0.3 Object Detection Finetuning Tutorial
# http://pytorch.org/tutorials/intermediate/torchvision_tutorial.html

import os
import time
from typing import List
import re

from absl import app, flags
import matplotlib
import matplotlib.pyplot as plt
import torch

from .datasets import BojaDataSet
from .engine import train_one_epoch, evaluate
from .._file_utils import create_output_dir, get_highest_numbered_file
from .. import _models
from .._s3_utils import (
    s3_bucket_exists,
    s3_upload_files,
    s3_download_dir,
)
from .transforms import ToTensor, RandomHorizontalFlip, Compose
from .train_utils import collate_fn
from .._settings import (
    DEFAULT_LOCAL_DATA_DIR,
    DEFAULT_S3_DATA_DIR,
    IMAGE_DIR_NAME,
    ANNOTATION_DIR_NAME,
    MANIFEST_DIR_NAME,
    MODEL_STATE_DIR_NAME,
    IMAGE_FILE_TYPE,
    ANNOTATION_FILE_TYPE,
    MANIFEST_FILE_TYPE,
    MODEL_STATE_FILE_TYPE,
    LABEL_FILE_NAME,
    LOGS_DIR_NAME,
    INVALID_ANNOTATION_FILE_IDENTIFIER,
    NETWORKS,
)

matplotlib.use("Agg")

AVERAGE_PRECISION_STAT_INDEX = 0
AVERAGE_RECALL_STAT_INDEX = 8

flags.DEFINE_string(
    "local_data_dir", DEFAULT_LOCAL_DATA_DIR, "Local data directory.",
)

flags.DEFINE_string(
    "s3_bucket_name", None, "S3 bucket to retrieve images from and upload manifest to."
)

flags.DEFINE_string(
    "s3_data_dir", DEFAULT_S3_DATA_DIR, "Prefix of the s3 data objects."
)

# Hyperparameters
flags.DEFINE_enum(
    "network", NETWORKS[0], NETWORKS, "The neural network to use for object detection",
)
flags.DEFINE_integer("num_epochs", 10, "The number of epochs to train the model for.")


def get_transform(train):
    transforms = []
    transforms.append(ToTensor())
    if train:
        transforms.append(RandomHorizontalFlip(0.5))
    return Compose(transforms)


def get_newest_manifest_path(manifest_dir_path: str) -> str:
    return get_highest_numbered_file(manifest_dir_path, MANIFEST_FILE_TYPE)


def main(unused_argv):

    start_time = int(time.time())

    use_s3 = True if flags.FLAGS.s3_bucket_name is not None else False

    if use_s3:
        if not s3_bucket_exists(flags.FLAGS.s3_bucket_name):
            use_s3 = False
            print(
                "Bucket: %s either does not exist or you do not have access to it"
                % flags.FLAGS.s3_bucket_name
            )
        else:
            print(
                "Bucket: %s exists and you have access to it"
                % flags.FLAGS.s3_bucket_name
            )

    if use_s3:
        # Download any new images from s3
        s3_download_dir(
            flags.FLAGS.s3_bucket_name,
            "/".join([flags.FLAGS.s3_data_dir, IMAGE_DIR_NAME]),
            os.path.join(flags.FLAGS.local_data_dir, IMAGE_DIR_NAME),
            IMAGE_FILE_TYPE,
        )

        # Download any new annotation files from s3
        s3_download_dir(
            flags.FLAGS.s3_bucket_name,
            "/".join([flags.FLAGS.s3_data_dir, ANNOTATION_DIR_NAME]),
            os.path.join(flags.FLAGS.local_data_dir, ANNOTATION_DIR_NAME),
            ANNOTATION_FILE_TYPE,
        )

        # Download any new manifests files from s3
        s3_download_dir(
            flags.FLAGS.s3_bucket_name,
            "/".join([flags.FLAGS.s3_data_dir, MANIFEST_DIR_NAME]),
            os.path.join(flags.FLAGS.local_data_dir, MANIFEST_DIR_NAME),
            MANIFEST_FILE_TYPE,
        )

    label_file_path = os.path.join(flags.FLAGS.local_data_dir, LABEL_FILE_NAME)
    if not os.path.isfile(label_file_path):
        print("Missing file %s" % label_file_path)
        return

    # read in the category labels
    labels = open(label_file_path).read().splitlines()

    if len(labels) == 0:
        print("No label categories found in %s" % label_file_path)
        return

    # add the background class
    labels.insert(0, "background")

    newest_manifest_file = get_newest_manifest_path(
        os.path.join(flags.FLAGS.local_data_dir, MANIFEST_DIR_NAME)
    )

    if newest_manifest_file is None:
        print(
            "Cannot find a manifest file in: %s"
            % (os.path.join(flags.FLAGS.local_data_dir, MANIFEST_DIR_NAME))
        )

    # train on the GPU or on the CPU, if a GPU is not available
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    print("Using device: ", device)

    num_classes = len(labels)
    # use our dataset and defined transformations

    dataset = BojaDataSet(
        os.path.join(flags.FLAGS.local_data_dir, IMAGE_DIR_NAME),
        os.path.join(flags.FLAGS.local_data_dir, ANNOTATION_DIR_NAME),
        newest_manifest_file,
        get_transform(train=True),
        labels,
    )

    dataset_test = BojaDataSet(
        os.path.join(flags.FLAGS.local_data_dir, IMAGE_DIR_NAME),
        os.path.join(flags.FLAGS.local_data_dir, ANNOTATION_DIR_NAME),
        newest_manifest_file,
        get_transform(train=False),
        labels,
    )

    # split the dataset in train and test set
    indices = torch.randperm(len(dataset)).tolist()

    # use 20 percent of the dataset for testing
    num_test = int(0.2 * len(dataset))

    dataset = torch.utils.data.Subset(dataset, indices[: -1 * num_test])
    dataset_test = torch.utils.data.Subset(dataset_test, indices[-1 * num_test :])

    print(
        "Training dataset size: %d, Testing dataset size: %d"
        % (len(dataset), len(dataset_test))
    )

    # define training and validation data loaders
    # data_loader = torch.utils.data.DataLoader(
    #     dataset, batch_size=2, shuffle=True, num_workers=4, collate_fn=utils.collate_fn
    # )

    data_loader = torch.utils.data.DataLoader(
        dataset, batch_size=1, shuffle=True, num_workers=1, collate_fn=collate_fn
    )

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1, shuffle=False, num_workers=1, collate_fn=collate_fn,
    )

    # get the model using our helper function
    model = _models.__dict__[flags.FLAGS.network](num_classes)

    # move model to the right device
    model.to(device)

    # construct an optimizer
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=0.005, momentum=0.9, weight_decay=0.0005)
    # and a learning rate scheduler
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=3, gamma=0.1)

    num_epochs = flags.FLAGS.num_epochs

    print("Training for %d epochs" % num_epochs)

    average_persision = []
    average_recall = []

    for epoch in range(num_epochs):
        # train for one epoch, printing every 10 iterations
        train_one_epoch(model, optimizer, data_loader, device, epoch, print_freq=10)
        # update the learning rate
        lr_scheduler.step()
        # evaluate on the test dataset
        eval_data = evaluate(model, data_loader_test, device=device)

        stats = eval_data.coco_eval["bbox"].stats
        average_persision.append(stats[AVERAGE_PRECISION_STAT_INDEX])
        average_recall.append(stats[AVERAGE_RECALL_STAT_INDEX])

    model_state_local_dir = os.path.join(
        flags.FLAGS.local_data_dir, MODEL_STATE_DIR_NAME
    )
    # Create model state directory if it does not exist yet
    create_output_dir(model_state_local_dir)
    run_name = "%s-%s" % (str(start_time), flags.FLAGS.network)

    model_state_file_path = os.path.join(
        model_state_local_dir, "%s.%s" % (run_name, MODEL_STATE_FILE_TYPE),
    )

    # Save the model state to a file
    torch.save(model.state_dict(), model_state_file_path)

    print("Model state saved at: %s" % model_state_file_path)

    plt.plot(average_persision, label="AP: IoU=0.50:0.95 maxDets=100")
    plt.plot(average_recall, label="AR: IoU=0.50:0.95 maxDets=100")
    plt.legend(loc="lower right")
    plt.title("Evaluation data from %s" % run_name)

    # Create log file directory if it does not exist yet
    log_image_local_dir = os.path.join(flags.FLAGS.local_data_dir, LOGS_DIR_NAME)
    create_output_dir(log_image_local_dir)

    log_file_name = "%s.jpg" % run_name
    log_file_path = os.path.join(log_image_local_dir, log_file_name)
    plt.savefig(log_file_path)

    print("Log file saved at: %s" % log_file_path)

    if use_s3:
        # Send the saved model and logs to S3
        s3_upload_files(
            flags.FLAGS.s3_bucket_name,
            [model_state_file_path],
            "/".join([flags.FLAGS.s3_data_dir, MODEL_STATE_DIR_NAME]),
        )
        s3_upload_files(
            flags.FLAGS.s3_bucket_name,
            [log_file_path],
            "/".join([flags.FLAGS.s3_data_dir, LOGS_DIR_NAME]),
        )

    print("Training complete")


if __name__ == "__main__":
    app.run(main)
