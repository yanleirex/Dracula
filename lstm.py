'''
Build a tweet sentiment analyzer
'''

from argparse import ArgumentParser
import logging

import cPickle as pkl
import time

from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from theano.tensor.shared_randomstreams import RandomStreams

from util import get_minibatches_idx
from modelio import load_pos_tagged_data, prepare_data, get_max_word_count, get_max_length, get_max_word_length

from nn_layers import *
from nn_lstm import lstm_layer, lstm_unmasked_layer, bidirectional_lstm_layer
from nn_params import *
from nn_optimizers import *
from nn_support import pred_error
from nn_serialization import zipp, unzip, load_params

import os.path
import pickle

# Set the random number generators' seeds for consistency
SEED = 123
numpy.random.seed(SEED)

def build_model(tparams, options, maxw, training=True):
    xc = tensor.matrix('xc', dtype='int8')

    xc.tag.test_value=numpy.asarray([[5, 5, 1], [5, 5, 1], [5, 5, 1]])
    mask = tensor.matrix('mask', dtype=config.floatX)
    mask.tag.test_value=numpy.asarray([[1, 1, 1], [1, 1, 1], [1, 1, 1]])
    wmask = tensor.ftensor4('wmask')
    y = tensor.matrix('y', dtype='int8')
    y.tag.test_value=numpy.asarray([[1, 1, 1], [1, 1, 1], [1, 1, 1]])
    y_mask = tensor.matrix('y_mask', dtype='int8')

    n_timesteps = xc.shape[0]
    n_samples = xc.shape[1]

    emb = embeddings_layer(xc, tparams['Cemb'], n_timesteps, n_samples, options['dim_proj'])

    dist = per_word_averaging_layer_distrib(emb, wmask, maxw)
    dist = dist.dimshuffle(1, 2, 0, 3)
    #dist = theano.printing.Print("dist", attrs=["shape"])(dist)
#    dist_mask = tensor.neq(dist, numpy_floatX(0.0))

    for i in range(options['letter_layers']):
        name = 'lstm_chars_%d' % (i + 1,)
#        def _step(x_, o_):
#            o_ = bidirectional_lstm_layer(tparams, x_, options, name)
#            return o_
#        dist = theano.scan(_step,
#                            sequences=[dist],
#                            outputs_info=[tensor.zeros_like(dist)])
        for j in range(8):
            tmp = bidirectional_lstm_layer(tparams, dist[:, :, j, :], options, name)
            #tmp = theano.printing.Print("tmp", attrs=["shape"])(tmp)
            dist = tensor.set_subtensor(dist[:, :, j, :], tmp)

    divider = tensor.cast(tensor.neq(dist, numpy_floatX(0.0)).sum(axis=0), theano.config.floatX)
    divider += tensor.eq(divider, numpy_floatX(0.0)) # Filter NaNs

    tmp = tensor.cast(dist.sum(axis=0), theano.config.floatX)
    tmp /= divider
    proj2 = tmp.dimshuffle(1, 0, 2)
    #proj2 = theano.printing.Print("proj2", attrs=["shape"])(proj2)

#    for i in range(options['letter_layers']):
#        name = 'lstm_chars_%d' % (i + 1,)
#        proj = bidirectional_lstm_layer(tparams, proj, options, name, mask=mask)

#    avg_per_word = per_word_averaging_layer(proj, wmask, maxw)
#    avg_per_word = avg_per_word.dimshuffle(1, 0, 2)

#    proj2 = avg_per_word
    for i in range(options['word_layers']):
        name = 'lstm_words_%d' % (i + 1,)
        proj2 = bidirectional_lstm_layer(tparams, proj2, options, name)

    pred = softmax_layer(proj2, tparams['U'], tparams['b'], y_mask, maxw, training)

    f_pred_prob = theano.function([xc, mask, wmask, y_mask], pred, name='f_pred_prob', on_unused_input='ignore')
    f_pred = theano.function([xc, mask, wmask, y_mask], pred.argmax(axis=2), name='f_pred', on_unused_input='ignore')

    def cost_scan_i(i, j, free_var):
        return -tensor.log(i[tensor.arange(n_samples), j] + 1e-8)

    cost, _ = theano.scan(cost_scan_i, outputs_info=None, sequences=[pred, y, tensor.arange(n_samples)])

    cost = cost.mean()

    return xc, mask, wmask, y, y_mask, f_pred_prob, f_pred, cost

def split_at(src, prop):
    src_chars, src_words, src_labels = [], [], []
    val_chars, val_words, val_labels = [], [], []
    fin = max(int(prop * len(src[0])), 1)
    print len(src[0]), prop, fin
    for i, ((c, w), l) in enumerate(zip(zip(src[0], src[1]), src[2])):
        if i < fin:
            val_chars.append(c)
            val_words.append(w)
            val_labels.append(l)
        else:
            src_chars.append(c)
            src_words.append(w)
            src_labels.append(l)
    return (src_chars, src_words, src_labels), (val_chars, val_words, val_labels)

def train_lstm(
    dim_proj_chars=16,  # character embedding dimension and LSTM number of hidden units.
    patience=4,  # Number of epoch to wait before early stop if no progress
    max_epochs=5000,  # The maximum number of epoch to run
    dispFreq=10,  # Display to stdout the training progress every N updates
    decay_c=0.0001,  # Weight decay for the classifier applied to the U weights.
    lrate=0.0001,  # Learning rate for sgd (not used for adadelta and rmsprop)
    optimizer=adadelta,  # sgd, adadelta and rmsprop available, sgd very hard to use, not recommanded (probably need momentum and decaying learning rate).
    encoder='lstm',  # TODO: can be removed must be lstm.
    saveto='lstm_model.npz',  # The best model will be saved there
    validFreq=450,  # Compute the validation error after this number of update.
    saveFreq=1110,  # Save the parameters after every saveFreq updates
    maxlen=100,  # Sequence longer then this get ignored
    batch_size=100,  # The batch size during training.
    valid_batch_size=64,  # The batch size used for validation/test set.
    dataset='imdb',

    # Parameter for extra option
    noise_std=0.,
    use_dropout=True,  # if False slightly faster, but worst test error
                       # This frequently need a bigger model.
    reload_model=None,  # Path to a saved model we want to start from.
    test_size=-1,  # If >0, we keep only this number of test example.
    pretrain = False, # If True, load some data from this argument
    # Use to keep track of feature enumeration
    char_dict = {},
    word_dict = {},
    pos_dict = {},
    word_layers=0,
    letter_layers=0
):

    # Model options
    model_options = locals().copy()
    print "model options", model_options

    if reload_model is not None:
        load_params(reload_model, model_options)
        char_dict = model_options['char_dict']
        word_dict = model_options['word_dict']
        pos_dict = model_options['pos_dict']

    # Load the training data
    print 'Loading data'
    #
    # Pre-populate the dictionaries
    #
    if not os.path.isfile("substitutions.pkl"):
        raise Exception("substitutions.pkl wasn't found, have you run substitution.py?")

    #load_pos_tagged_data("Data/Brown.conll", char_dict, word_dict, pos_dict)
    #load_pos_tagged_data("Data/TweeboOct27.conll", char_dict, word_dict, pos_dict)
    #load_pos_tagged_data("Data/TweeboDaily547.conll", char_dict, word_dict, pos_dict)

    load_pos_tagged_data("Data/Gate.conll", char_dict, word_dict, pos_dict)

    with open("substitutions.pkl", "rb") as fin:
        word_dict = pickle.load(fin)

    max_word_count = 0
    max_word_length = 0
    max_length = 0
    if not pretrain:
        # Now load the data for real
        data = load_pos_tagged_data("Data/Gate-Train.conll", char_dict, word_dict, pos_dict, 0)
        train, eval = split_at(data, 0.30)
        test, valid = split_at(eval, 0.50)
        max_word_count = max(max_word_count, \
        get_max_word_count("Data/Gate-Train.conll"))
        max_word_length = max(max_word_length, \
        get_max_word_length("Data/Gate-Train.conll"))
        max_length = max(max_word_length, get_max_length("Data/Gate-Train.conll"))
        batch_size = 100
    else:
        # Pre-populate
        test = load_pos_tagged_data("Data/Brown.conll", char_dict, word_dict, pos_dict)
        max_word_count = get_max_word_count("Data/Brown.conll")
        max_word_length = get_max_word_length("Data/Brown.conll")
        max_length = get_max_length("Data/Brown.conll")
        train, valid = split_at(test, 0.05)

    ydim = numpy.max(numpy.amax(train[2])) + 1
    print "ydim =", ydim

    model_options['ydim'] = ydim
    model_options['n_chars'] = len(char_dict)+1
    model_options['n_words'] = len(word_dict)+1

    model_options['word_dict'] = word_dict
    model_options['char_dict'] = char_dict
    model_options['pos_dict'] = pos_dict

    # This create the initial parameters as numpy ndarrays.
    # Dict name (string) -> numpy ndarray
    params = init_params(model_options, reload_model is not None)

    logging.info('Building model')

    # This create Theano Shared Variable from the parameters.
    # Dict name (string) -> Theano Tensor Shared Variable
    # params and tparams have different copy of the weights.
    tparams = init_tparams(params)

    # use_noise is for dropout
    (xc, mask, wmask,
     y, y_mask, f_pred_prob, f_pred, cost) = build_model(tparams, model_options, max_word_count)

    if decay_c > 0.:
        decay_c = theano.shared(numpy_floatX(decay_c), name='decay_c')
        weight_decay = 0.
        weight_decay += (tparams['U'] ** 2).sum()
        weight_decay *= decay_c
        cost += weight_decay

    f_cost = theano.function([xc, mask, wmask, y, y_mask], cost, name='f_cost', on_unused_input='warn')

    grads = tensor.grad(cost, wrt=tparams.values())
    f_grad = theano.function([xc, mask, wmask, y, y_mask], grads, name='f_grad', on_unused_input='warn')

    lr = tensor.scalar(name='lr')
    f_grad_shared, f_update = optimizer(lr, tparams, grads,
                                        xc, mask, wmask, y, y_mask, cost)

    kf_valid = get_minibatches_idx(len(valid[0]), valid_batch_size)
    kf_test = get_minibatches_idx(len(test[0]), valid_batch_size)

    logging.info("%d train examples" % len(train[0]))
    logging.info("%d valid examples" % len(valid[0]))
    logging.info("%d test examples" % len(test[0]))

    history_errs = []
    best_p = None
    bad_count = 0

    if validFreq == -1:
        validFreq = len(train[0]) / batch_size
        logging.info("validFreq auto set to %d", validFreq)
    if saveFreq == -1:
        saveFreq = len(train[0]) / batch_size
        logging.info("saveFreq auto set to %d", saveFreq)

    uidx = 0  # the number of update done
    estop = False  # early stop
    start_time = time.clock()

    trng = RandomStreams(123)

    try:
        for eidx in xrange(max_epochs):
            n_samples = 0

            # Get new shuffled index for the training set.
            kf = get_minibatches_idx(len(train[0]), batch_size, shuffle=True)

            for _, train_index in kf:
                uidx += 1

                # Select the random examples for this minibatch
                y = [train[2][t] for t in train_index]
                x_c = [train[0][t] for t in train_index]
                x_w = [train[1][t] for t in train_index]

                # Get the data in numpy.ndarray format
                # This swap the axis!
                # Return something of shape (minibatch maxlen, n samples)
                n_proj = dim_proj_chars# + dim_proj_words
                xc, xw, mask, wmask, y, y_mask = prepare_data(x_c, x_w, y,
                                                              max_length, max_word_count,
                                                              max_word_length, n_proj)
                n_samples += xc.shape[1]

                assert xc.shape == xw.shape

                cost = f_grad_shared(xc, mask, wmask, y, y_mask)
                f_update(lrate)

                if numpy.isnan(cost) or numpy.isinf(cost):
                    logging.error('NaN detected (bad cost)')
                    return 1., 1., 1.

                if numpy.mod(uidx, dispFreq) == 0:
                    logging.info('Epoch %d, Update %d, Cost %.4f', eidx, uidx, cost)

                if saveto and numpy.mod(uidx, saveFreq) == 0:
                    logging.info('Saving to %s', saveto)

                    if best_p is not None:
                        params = best_p
                    else:
                        params = unzip(tparams)
                    numpy.savez(saveto, history_errs=history_errs, **params)
                    pkl.dump(model_options, open('%s.pkl' % saveto, 'wb'), -1)
                    logging.info('Incremental save complete')

                if numpy.mod(uidx, validFreq) == 0:
                    valid_err = pred_error(f_pred, prepare_data, valid, kf_valid, max_length, max_word_count, max_word_length, n_proj)

                    if not pretrain:
                        #train_err = pred_error(f_pred, prepare_data, train, kf, 140, max_word_count, max_word_length, n_proj)
                        test_err = pred_error(f_pred, prepare_data, test, kf_test, max_length, max_word_count, max_word_length, n_proj)
                        history_errs.append([valid_err, test_err])
                    else:
                        history_errs.append([valid_err, 0.0])

                    if (uidx == 0 or
                        valid_err <= numpy.array(history_errs)[:,
                                                               0].min()):

                        best_p = unzip(tparams)
                        bad_counter = 0
                    if not pretrain:
                        logging.info("Valid %.4f, Test %.4f",
                                     100*(1-valid_err), 100*(1-test_err))
                    else:
                        logging.info("Valid %.4f", 100 * (1-valid_err))

                    if (len(history_errs) > patience and
                        valid_err >= numpy.array(history_errs)[:-patience,
                                                               0].min()):
                        bad_counter += 1
                        if bad_counter > patience:
                            logging.warn('Early Stop!')
                            estop = True
                            break

            logging.info('Seen %d samples', n_samples)

            if estop:
                break

    except KeyboardInterrupt:
        logging.warn("Training interrupted")

    end_time = time.clock()
    if best_p is not None:
        zipp(best_p, tparams)
    else:
        best_p = unzip(tparams)

    kf_train_sorted = get_minibatches_idx(len(train[0]), batch_size)
    train_err = pred_error(f_pred, prepare_data, train, kf_train_sorted, max_length, max_word_count, max_word_length, n_proj)
    valid_err = pred_error(f_pred, prepare_data, valid, kf_valid, max_length, max_word_count, max_word_length, n_proj)
    test_err = pred_error(f_pred, prepare_data, test, kf_test, max_length, max_word_count, max_word_length, n_proj)

    logging.info("Train %.4f, Valid %.4f, Test %.4f",
                                     100*(1-train_err), 100*(1-valid_err), 100*(1-test_err))

    if saveto:
        logging.info("Saving to %s...", saveto)
        numpy.savez(saveto, train_err=train_err,
                    valid_err=valid_err, test_err=test_err,
                    history_errs=history_errs, **best_p)
    logging.info('The code run for %d epochs, with %f sec/epochs',
        (eidx + 1), (end_time - start_time) / (1. * (eidx + 1)))

    logging.info('Training took %.1fs',
                          (end_time - start_time))
    return train_err, valid_err, test_err


if __name__ == '__main__':

    logging.basicConfig(level=logging.DEBUG)

    a = ArgumentParser("Train/Evaluate the LSTM model")
    a.add_argument("--model", help="Load an existing model")
    a.add_argument("--max-epochs", type=int, default=1000)
    a.add_argument("--pretrain", help="Divide a 90-10 training/eval thing", action="store_true")
    a.add_argument("--words", help="Number of recurrent layers (word level)", type=int, default=0)
    a.add_argument("--letters", help="Number of recurrent layers (letter level)", type=int, default=0)

    p = a.parse_args()

    # See function train for all possible parameter and there definition.
    train_lstm(
        max_epochs=p.max_epochs,
        reload_model=p.model,
        pretrain=p.pretrain,
        word_layers=p.words,
        letter_layers=p.letters
    )
