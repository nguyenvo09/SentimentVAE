import glob
import sys
import time

import numpy as np
import tensorflow as tf

from beamsearch import BeamDecoder
from config import cfg
from encdec import EncoderDecoderModel
from reader import Reader, Vocab
import rnncell
import utils


def call_mle_session(session, model, batch, summarize=False, get_z=False,
                     get_z_mean=False):
    '''Use the session to run the model on the batch data.'''
    f_dict = {model.data: batch[0],
              model.data_dropped: batch[3],
              model.lengths: batch[1]}
    if cfg.use_labels:
        f_dict[model.labels] = batch[2]

    ops = [model.nll, model.kld, model.mutinfo, model.cost]
    if summarize:
        ops.extend([model.summary(), model.global_step])
    if get_z:
        ops.append(model.z)
    if get_z_mean:
        ops.append(model.z_mean)
    ops.append(model.train_op)
    return session.run(ops, f_dict)[:-1]


def save_model(session, saver, perp, kld, cur_iters):
    '''Save model file.'''
    save_file = cfg.save_file
    if not cfg.save_overwrite:
        save_file = save_file + '.' + str(cur_iters)
    print("Saving model (epoch perplexity: %.3f, kl_divergence: %.3f) ..." % (perp, kld))
    save_file = saver.save(session, save_file)
    print("Saved to", save_file)


def generate_sentences(model, vocab, beam_size):
    cell = rnncell.SoftmaxWrapper(model.decode_cell, model.softmax_w, model.softmax_b,
                                  stddev=cfg.decoding_noise)
    initial_state = model.decode_initial
    if cfg.decoder_inputs:
        initial_input = tf.nn.embedding_lookup(model.embedding,
                                               tf.constant(vocab.sos_index, tf.int32,
                                                           [cfg.batch_size]))
    else:
        initial_input = tf.zeros([cfg.batch_size, 1])
    if cfg.use_labels:
        label_embs = tf.nn.embedding_lookup(model.label_embedding,
                                            model.labels - min(vocab.labels))

        batch_concat = tf.concat(1, [model.z_transformed, label_embs])
    else:
        batch_concat = model.z_transformed
    min_op = model.lengths
    beam_decoder = BeamDecoder(len(vocab.vocab), batch_concat, beam_size=beam_size,
                               stop_token=vocab.eos_index, max_len=cfg.max_gen_length,
                               min_op=min_op, length_penalty=cfg.length_penalty)

    if cfg.decoder_inputs:
        loop_function = lambda prev_symbol, i: tf.nn.embedding_lookup(model.embedding,
                                                                      prev_symbol)
    else:
        loop_function = lambda prev_symbol, i: tf.zeros([tf.shape(prev_symbol)[0], 1])
    _, final_state = tf.nn.seq2seq.rnn_decoder(
                         [beam_decoder.wrap_input(initial_input)] +
                         [None] * (cfg.max_gen_length - 1),
                         beam_decoder.wrap_state(initial_state),
                         beam_decoder.wrap_cell(cell),
                         loop_function=loop_function,
                         scope='Decoder/RNN'
                     )
    return beam_decoder.unwrap_output_dense(final_state)


def show_reconstructions(session, model, generate_op, batch, vocab, z):
    print('\nTrue output')
    utils.display_sentences(batch[0][:, 1:], vocab)
    print('Sentences generated from encodings')
    f_dict = {model.z: z, model.lengths: batch[1]}
    if cfg.use_labels:
        f_dict[model.labels] = batch[2]
    output = session.run(generate_op, f_dict)
    utils.display_sentences(output, vocab, right_aligned=True)


def run_epoch(epoch, session, model, generator, batch_loader, vocab, saver, steps,
              max_steps, generate_op, summary_writer=None):
    '''Runs the model on the given data for an epoch.'''
    start_time = time.time()
    word_count = 0.0
    nlls = 0.0
    klds = 0.0
    lls = 0.0
    costs = 0.0
    iters = 0

    for step, batch in enumerate(batch_loader):
        cur_iters = steps + step
        drop_prob = utils.linear_interpolation(cfg.init_dropout, cfg.word_dropout,
                                               cfg.dropout_start, cfg.dropout_finish,
                                               cur_iters)
        dropped = utils.word_dropout(batch[0], batch[1], vocab, drop_prob)
        batch = batch + (dropped,)

        print_now = cfg.print_every != 0 and step % cfg.print_every == 0 and step > 0
        display_now = cfg.autoencoder and cfg.display_every != 0 and \
                      step % cfg.display_every == 0
        summarize_now = print_now and summary_writer is not None and step > 0
        ret = call_mle_session(session, model, batch, summarize=summarize_now,
                               get_z=display_now)
        nll, kld, mutinfo, cost = ret[:4]
        ll = -nll - kld
        if summarize_now:
            summary_str, gstep = ret[4:6]
        if display_now:
            z = ret[-1]
        sentence_length = np.sum(batch[0] != 0) // cfg.batch_size
        word_count += sentence_length
        kld_weight = session.run(model.kld_weight)
        nlls += nll
        klds += kld
        lls += ll
        costs += cost
        iters += sentence_length
        if print_now:
            print("%d: %d (%d)  perplexity: %.3f  mle_loss: %.4f  kl_divergence: %.4f  "
                  "mutinfo_loss: %.4f  ll: %.4f  cost: %.4f  kld_weight: %.4f  "
                  "speed: %.0f wps" % (epoch + 1, step, cur_iters,
                   np.exp(nll/sentence_length), nll, kld, mutinfo, ll, cost, kld_weight,
                   word_count * cfg.batch_size / (time.time() - start_time)))
            if summary_writer is not None:
                summary_writer.add_summary(summary_str, gstep)
        if cfg.debug:
            print()

        if display_now:
            show_reconstructions(session, generator, generate_op, batch, vocab, z)

        if saver is not None and cur_iters and cfg.save_every > 0 and \
                cur_iters % cfg.save_every == 0:
            save_model(session, saver, np.exp(nlls / iters), np.exp(klds / (step + 1)),
                       cur_iters)
        if max_steps > 0 and cur_iters >= max_steps:
            break

    perp = np.exp(nlls / iters)
    kld = klds / step
    ll = lls / step
    cur_iters = steps + step
    if saver is not None and cfg.save_every < 0:
        save_model(session, saver, perp, kld, cur_iters)
    return perp, kld, ll, cur_iters


def main(_):
    vocab = Vocab()
    vocab.load_from_pickle()
    reader = Reader(vocab)

    config_proto = tf.ConfigProto()
    # config_proto.gpu_options.allow_growth = True

    if not cfg.training and not cfg.save_overwrite:
        load_files = [f for f in glob.glob(cfg.load_file + '.*')
                      if not f.endswith('meta')]
        load_files = sorted(load_files, key=lambda x: int(x[len(cfg.load_file)+1:]))
    else:
        load_files = [cfg.load_file]
    if not cfg.training:
        test_losses = []
    for load_file in load_files:
        with tf.Graph().as_default(), tf.Session(config=config_proto) as session:
            with tf.variable_scope("Model") as scope:
                if cfg.training:
                    with tf.name_scope('training'):
                        model = EncoderDecoderModel(vocab, True)
                        scope.reuse_variables()
                    with tf.name_scope('evaluation'):
                        eval_model = EncoderDecoderModel(vocab, False)
                else:
                    test_model = EncoderDecoderModel(vocab, False)
                    scope.reuse_variables()
                with tf.name_scope('generator'):
                    generator = EncoderDecoderModel(vocab, False, True)
                with tf.name_scope('beam_search'):
                    if cfg.autoencoder:
                        generate_op = generate_sentences(generator, vocab, cfg.beam_size)
                    else:
                        generate_op = tf.no_op()
            saver = tf.train.Saver(max_to_keep=None)
            summary_writer = tf.train.SummaryWriter('./summary', session.graph)
            steps = 0
            try:
                # try to restore a saved model file
                saver.restore(session, load_file)
                print("\nModel restored from", load_file)
                with tf.variable_scope("Model", reuse=True):
                    steps = int(session.run(tf.get_variable("global_step")))
                print('Global step', steps)
            except ValueError:
                if cfg.training:
                    tf.initialize_all_variables().run()
                    print("No loadable model file, new model initialized.")
                else:
                    print("You need to provide a valid model file for testing!")
                    sys.exit(1)
            if cfg.training:
                train_losses = []
                valid_losses = []
                model.assign_lr(session, cfg.learning_rate)
                for i in range(cfg.max_epoch):
                    print("\nEpoch: %d  Learning rate: %.5f" % (i + 1,
                                                                session.run(model.lr)))
                    perplexity, kld, ll, steps = run_epoch(i, session, model, generator,
                                                           reader.training(), vocab,
                                                           saver, steps, cfg.max_steps,
                                                           generate_op, summary_writer)
                    print("Epoch: %d Train Perplexity: %.3f, KL Divergence: %.3f, "
                          "LL: %.3f" % (i + 1, perplexity, kld, ll))
                    train_losses.append((perplexity, kld, ll))
                    if cfg.validate_every > 0 and (i + 1) % cfg.validate_every == 0:
                        perplexity, kld, ll, _ = run_epoch(i, session,
                                                    eval_model, generator,
                                                    reader.validation(cfg.val_ll_samples),
                                                    vocab, None, 0, -1, generate_op, None)
                        print("Epoch: %d Validation Perplexity: %.3f, "
                              "KL Divergence: %.3f, LL: %.3f" % (i + 1, perplexity, kld,
                                                                 ll))
                        valid_losses.append((perplexity, kld, ll))
                    else:
                        valid_losses.append(None)
                    print('Train:', train_losses)
                    print('Valid:', valid_losses)
                    if steps >= cfg.max_steps:
                        break
            else:
                if cfg.test_validation:
                    batch_loader = reader.validation(cfg.test_ll_samples)
                else:
                    batch_loader = reader.testing(cfg.test_ll_samples)
                print('\nTesting')
                perplexity, kld, ll, _ = run_epoch(steps, session, test_model, generator,
                                                   batch_loader, vocab, None, 0,
                                                   cfg.max_steps, generate_op, None)
                print("Test Perplexity: %.3f, KL Divergence: %.3f, "
                      "LL: %.3f" % (perplexity, kld, ll))

                test_losses.append((steps, (perplexity, kld, ll)))
                print('Test:', test_losses)
                test_model = None


if __name__ == "__main__":
    tf.app.run()
