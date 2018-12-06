import numpy as np

import argparse
import pickle
import os
import shutil

import keras
from keras import backend as K

import utils
from datasets import get_data_generator



def cls_model(embed_model, num_classes, cls_base = None):
    """ Appends a classifier to an embedding model.

    # Arguments:

    - embed_model: Base model generating image features.

    - num_classes: Number of classes.

    - cls_base: Optionally, the name of the layer in `embed_model` that will be used for extracting embeddings.
                If set to None, the final output of the model will be used.
    
    # Returns:
        a new model that extends `embed_model` with a ReLU activation, batch normalization, and a fully-connected
        classifier with softmax activation. This model will have two outputs: the original output of the `embed_model`
        and the output of the appended classifier.
    """
    
    if cls_base is None:
        base = embed_model.output
    else:
        try:
            base = embed_model.layers[int(cls_base)].output
        except ValueError:
            base = embed_model.get_layer(cls_base).output
    
    x = keras.layers.Activation('relu')(base)
    x = keras.layers.BatchNormalization()(x)
    x = keras.layers.Dense(num_classes, activation = 'softmax', kernel_regularizer = keras.regularizers.l2(5e-4), name = 'prob')(x)
    return keras.models.Model(embed_model.inputs, [embed_model.output, x])


def transform_inputs(X, y, embedding, num_classes = None):
    
    return (X, embedding[y]) if num_classes is None else (X, [embedding[y], keras.utils.to_categorical(y, num_classes)])



if __name__ == '__main__':

    # Parse arguments
    parser = argparse.ArgumentParser(description = 'Learns to map images onto class embeddings.', formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    arggroup = parser.add_argument_group('Data parameters')
    arggroup.add_argument('--dataset', type = str, required = True, help = 'Training dataset. See README.md for a list of available datasets.')
    arggroup.add_argument('--data_root', type = str, required = True, help = 'Root directory of the dataset.')
    arggroup.add_argument('--embedding', type = str, required = True, help = 'Path to a pickle dump of embeddings generated by compute_class_embeddings.py.')
    arggroup = parser.add_argument_group('Training parameters')
    arggroup.add_argument('--architecture', type = str, default = 'simple', choices = utils.ARCHITECTURES, help = 'Type of network architecture.')
    arggroup.add_argument('--loss', type = str, default = 'inv_corr', choices = ['mse', 'inv_corr', 'unnorm_corr', 'softmax_corr'],
                          help = 'Loss function for learning embeddings. Use "mse" (mean squared error) for distance-based and "inv_corr" (negated dot product) for similarity-based L2-normalized embeddings. '
                                 '"unnorm_corr" and "softmax_corr" are the same as "inv_corr", but the first does not perform L2-normalization and the latter performs softmax activation instead.')
    arggroup.add_argument('--cls_weight', type = float, default = 0.0, help = 'If set to a positive value, an additional classification layer will be added and this parameter specifies the weight of the softmax loss.')
    arggroup.add_argument('--cls_base', type = str, default = None, help = 'Name or index of the layer that the classification layer should be based on. If not specified, the final embedding layer will be used.')
    arggroup.add_argument('--lr_schedule', type = str, default = 'SGDR', choices = utils.LR_SCHEDULES, help = 'Type of learning rate schedule.')
    arggroup.add_argument('--clipgrad', type = float, default = 10.0, help = 'Gradient norm clipping.')
    arggroup.add_argument('--max_decay', type = float, default = 0.0, help = 'Learning Rate decay at the end of training.')
    arggroup.add_argument('--nesterov', action = 'store_true', default = False, help = 'Use Nesterov momentum instead of standard momentum.')
    arggroup.add_argument('--epochs', type = int, default = None, help = 'Number of training epochs.')
    arggroup.add_argument('--batch_size', type = int, default = 100, help = 'Batch size.')
    arggroup.add_argument('--val_batch_size', type = int, default = None, help = 'Validation batch size.')
    arggroup.add_argument('--snapshot', type = str, default = None, help = 'Path where snapshots should be stored after every epoch. If existing, it will be used to resume training.')
    arggroup.add_argument('--initial_epoch', type = int, default = 0, help = 'Initial epoch for resuming training from snapshot.')
    arggroup.add_argument('--finetune', type = str, default = None, help = 'Path to pre-trained weights to be fine-tuned (will be loaded by layer name).')
    arggroup.add_argument('--finetune_init', type = int, default = 8, help = 'Number of initial epochs for training just the new layers before fine-tuning.')
    arggroup.add_argument('--gpus', type = int, default = 1, help = 'Number of GPUs to be used.')
    arggroup.add_argument('--read_workers', type = int, default = 8, help = 'Number of parallel data pre-processing processes.')
    arggroup.add_argument('--queue_size', type = int, default = 100, help = 'Maximum size of data queue.')
    arggroup.add_argument('--gpu_merge', action = 'store_true', default = False, help = 'Merge weights on the GPU.')
    arggroup = parser.add_argument_group('Output parameters')
    arggroup.add_argument('--model_dump', type = str, default = None, help = 'Filename where the learned model definition and weights should be written to.')
    arggroup.add_argument('--weight_dump', type = str, default = None, help = 'Filename where the learned model weights should be written to (without model definition).')
    arggroup.add_argument('--feature_dump', type = str, default = None, help = 'Filename where learned embeddings for test images should be written to.')
    arggroup.add_argument('--log_dir', type = str, default = None, help = 'Tensorboard log directory.')
    arggroup.add_argument('--no_progress', action = 'store_true', default = False, help = 'Do not display training progress, but just the final performance.')
    utils.add_lr_schedule_arguments(parser)

    args = parser.parse_args()
    
    if args.val_batch_size is None:
        args.val_batch_size = args.batch_size

    # Configure environment
    K.set_session(K.tf.Session(config = K.tf.ConfigProto(gpu_options = { 'allow_growth' : True })))

    # Load class embeddings
    with open(args.embedding, 'rb') as pf:
        embedding = pickle.load(pf)
        embed_labels = embedding['ind2label']
        embedding = embedding['embedding']

    # Load dataset
    data_generator = get_data_generator(args.dataset, args.data_root, classes = embed_labels)

    # Construct and train model
    embedding_layer_name = 'embedding'
    if (args.gpus <= 1) or args.gpu_merge:
        if args.snapshot and os.path.exists(args.snapshot):
            print('Resuming from snapshot {}'.format(args.snapshot))
            model = keras.models.load_model(args.snapshot, custom_objects = utils.get_custom_objects(args.architecture), compile = False)
        else:
            embed_model = utils.build_network(embedding.shape[1], args.architecture)
            model = embed_model
            if args.loss == 'inv_corr':
                model = keras.models.Model(model.inputs, keras.layers.Lambda(utils.l2norm, name = 'l2norm')(model.output))
                embedding_layer_name = 'l2norm'
            elif args.loss == 'softmax_corr':
                model = keras.models.Model(model.inputs, keras.layers.Activation('softmax', name = 'softmax')(model.output))
                embedding_layer_name = 'softmax'
            if args.cls_weight > 0:
                model = cls_model(model, data_generator.num_classes, args.cls_base)
        par_model = model if args.gpus <= 1 else keras.utils.multi_gpu_model(model, gpus = args.gpus, cpu_merge = False)
    else:
        with K.tf.device('/cpu:0'):
            if args.snapshot and os.path.exists(args.snapshot):
                print('Resuming from snapshot {}'.format(args.snapshot))
                model = keras.models.load_model(args.snapshot, custom_objects = utils.get_custom_objects(args.architecture), compile = False)
            else:
                embed_model = utils.build_network(embedding.shape[1], args.architecture)
                model = embed_model
                if args.loss == 'inv_corr':
                    model = keras.models.Model(model.inputs, keras.layers.Lambda(utils.l2norm, name = 'l2norm')(model.output))
                    embedding_layer_name = 'l2norm'
                elif args.loss == 'softmax_corr':
                    model = keras.models.Model(model.inputs, keras.layers.Activation('softmax', name = 'softmax')(model.output))
                    embedding_layer_name = 'softmax'
                if args.cls_weight > 0:
                    model = cls_model(model, data_generator.num_classes, args.cls_base)
        par_model = keras.utils.multi_gpu_model(model, gpus = args.gpus)
    
    if not args.no_progress:
        model.summary()
    
    batch_transform_kwargs = {
        'embedding' : embedding,
        'num_classes' : data_generator.num_classes if args.cls_weight > 0 else None
    }
    if args.loss.endswith('_corr'):
        loss = utils.inv_correlation
        metric = 'accuracy' if args.loss == 'softmax_corr' else utils.nn_accuracy(embedding, dot_prod_sim = True)
    else:
        loss = utils.squared_distance
        metric = utils.nn_accuracy(embedding, dot_prod_sim = False)
    
    # Load pre-trained weights and train last layer for a few epochs
    if args.finetune:
        print('Loading pre-trained weights from {}'.format(args.finetune))
        model.load_weights(args.finetune, by_name=True, skip_mismatch=True)
        if args.finetune_init > 0:
            print('Pre-training new layers')
            for layer in model.layers:
                layer.trainable = (layer.name in ('embedding', 'prob'))
            embed_model.layers[-1].trainable = True
            if args.cls_weight > 0:
                par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, momentum=0.9, nesterov=args.nesterov, clipnorm = args.clipgrad),
                                loss = { embedding_layer_name : loss, 'prob' : 'categorical_crossentropy' },
                                loss_weights = { embedding_layer_name : 1.0, 'prob' : args.cls_weight },
                                metrics = { embedding_layer_name : metric, 'prob' : 'accuracy' })
            else:
                par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, momentum=0.9, nesterov=args.nesterov, clipnorm = args.clipgrad),
                                loss = loss,
                                metrics = [metric])
            par_model.fit_generator(
                    data_generator.train_sequence(args.batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
                    validation_data = data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
                    epochs = args.finetune_init, verbose = not args.no_progress,
                    max_queue_size = args.queue_size, workers = args.read_workers, use_multiprocessing = True)
            for layer in model.layers:
                layer.trainable = True
            print('Full model training')

    # Train model
    callbacks, num_epochs = utils.get_lr_schedule(args.lr_schedule, data_generator.num_train, args.batch_size, schedule_args = { arg_name : arg_val for arg_name, arg_val in vars(args).items() if arg_val is not None })

    if args.log_dir:
        if os.path.isdir(args.log_dir):
            shutil.rmtree(args.log_dir, ignore_errors = True)
        callbacks.append(keras.callbacks.TensorBoard(log_dir = args.log_dir, write_graph = False))
    
    if args.snapshot:
        callbacks.append(keras.callbacks.ModelCheckpoint(args.snapshot) if args.gpus <= 1 else utils.TemplateModelCheckpoint(model, args.snapshot))

    if args.max_decay > 0:
        decay = (1.0/args.max_decay - 1) / ((data_generator.num_train // args.batch_size) * (args.epochs if args.epochs else num_epochs))
    else:
        decay = 0.0
    if args.cls_weight > 0:
        par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, decay=decay, momentum=0.9, nesterov=args.nesterov, clipnorm = args.clipgrad),
                          loss = { embedding_layer_name : loss, 'prob' : 'categorical_crossentropy' },
                          loss_weights = { embedding_layer_name : 1.0, 'prob' : args.cls_weight },
                          metrics = { embedding_layer_name : metric, 'prob' : 'accuracy' })
    else:
        par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, decay=decay, momentum=0.9, nesterov=args.nesterov, clipnorm = args.clipgrad),
                          loss = loss,
                          metrics = [metric])

    par_model.fit_generator(
              data_generator.train_sequence(args.batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
              validation_data = data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
              epochs = args.epochs if args.epochs else num_epochs, initial_epoch = args.initial_epoch,
              callbacks = callbacks, verbose = not args.no_progress,
              max_queue_size = args.queue_size, workers = args.read_workers, use_multiprocessing = True)

    # Evaluate final performance
    print(par_model.evaluate_generator(data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs)))
    if args.cls_weight > 0:
        test_pred = par_model.predict_generator(data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs))[1].argmax(axis=-1)
        class_freq = np.bincount(data_generator.labels_test)
        print('Average Accuracy: {:.4f}'.format(
            ((test_pred == np.asarray(data_generator.labels_test)).astype(np.float) / class_freq[np.asarray(data_generator.labels_test)]).sum() / len(class_freq)
        ))

    # Save model
    if args.weight_dump:
        try:
            model.save_weights(args.weight_dump)
        except Exception as e:
            print('An error occurred while saving the model weights: {}'.format(e))
    if args.model_dump:
        try:
            model.save(args.model_dump)
        except Exception as e:
            print('An error occurred while saving the model: {}'.format(e))

    # Save test image embeddings
    if args.feature_dump:
        pred_features = par_model.predict_generator(data_generator.flow_test(1, False), data_generator.num_test)
        if args.cls_weight > 0:
            pred_features = pred_features[0]
        with open(args.feature_dump,'wb') as dump_file:
            pickle.dump({ 'feat' : dict(enumerate(pred_features)) }, dump_file)
