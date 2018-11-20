# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""
Dialogue runner class. Implementes communication between two Agents.
"""
import sys
import pdb
import logging
import numpy as np

from metric import MetricsContainer
import data
import utils
import domain
from torch.autograd import Variable
import torch, random, copy

logging.basicConfig(format='%(asctime)s : %(levelname)s : %(filename)s : %(message)s', level=logging.INFO)

class DialogLogger(object):
    """Logger for a dialogue."""
    CODE2ITEM = [
        ('item0', 'book'),
        ('item1', 'hat'),
        ('item2', 'ball'),
    ]

    def __init__(self, verbose=False, log_file=None, append=False):
        self.logs = []
        if verbose:
            self.logs.append(sys.stderr)
        if log_file:
            flags = 'a' if append else 'w'
            self.logs.append(open(log_file, flags))

    def _dump(self, s, forced=False):
        for log in self.logs:
            print(s, file=log)
            log.flush()
        if forced:
            print(s, file=sys.stdout)
            sys.stdout.flush()

    def _dump_with_name(self, name, s):
        self._dump('{0: <5} : {1}'.format(name, s))

    def dump_ctx(self, name, ctx):
        assert len(ctx) == 6, 'we expect 3 objects'
        s = ' '.join(['%s=(count:%s value:%s)' % (self.CODE2ITEM[i][1], ctx[2 * i], ctx[2 * i + 1]) \
            for i in range(3)])
        self._dump_with_name(name, s)

    def dump_sent(self, name, sent):
        self._dump_with_name(name, ' '.join(sent))

    def dump_choice(self, name, choice):
        def rep(w):
            p = w.split('=')
            if len(p) == 2:
                for k, v in self.CODE2ITEM:
                    if p[0] == k:
                        return '%s=%s' % (v, p[1])
            return w

        self._dump_with_name(name, ' '.join([rep(c) for c in choice]))

    def dump_agreement(self, agree):
        self._dump('Agreement!' if agree else 'Disagreement?!')

    def dump_reward(self, name, agree, reward):
        if agree:
            self._dump_with_name(name, '%d points' % reward)
        else:
            self._dump_with_name(name, '0 (potential %d)' % reward)

    def dump(self, s, forced=False):
        self._dump(s, forced=forced)


class DialogSelfTrainLogger(DialogLogger):
    """This logger is used to produce new training data from selfplaying."""
    def __init__(self, verbose=False, log_file=None):
        super(DialogSelfTrainLogger, self).__init__(verbose, log_file)
        self.name2example = {}
        self.name2choice = {}

    def _dump_with_name(self, name, sent):
        for n in self.name2example:
            if n == name:
                self.name2example[n] += " YOU: "
            else:
                self.name2example[n] += " THEM: "

            self.name2example[n] += sent

    def dump_ctx(self, name, ctx):
        self.name2example[name] = ' '.join(ctx)

    def dump_choice(self, name, choice):
        self.name2choice[name] = ' '.join(choice)

    def dump_agreement(self, agree):
        if agree:
            for name in self.name2example:
                for other_name in self.name2example:
                    if name != other_name:
                        self.name2example[name] += ' ' + self.name2choice[name]
                        self.name2example[name] += ' ' + self.name2choice[other_name]
                        self._dump(self.name2example[name])

    def dump_reward(self, name, agree, reward):
        pass


class Dialog(object):
    """Dialogue runner."""
    def __init__(self, agents, args):
        # for now we only suppport dialog of 2 agents
        assert len(agents) == 2
        self.agents = agents
        self.args = args
        self.domain = domain.get_domain(args.domain)
        self.metrics = MetricsContainer()
        self._register_metrics()

    def _register_metrics(self):
        """Registers valuable metrics."""
        self.metrics.register_average('dialog_len')
        self.metrics.register_average('sent_len')
        self.metrics.register_percentage('agree')
        self.metrics.register_average('advantage')
        self.metrics.register_time('time')
        self.metrics.register_average('comb_rew')
        for agent in self.agents:
            self.metrics.register_average('%s_rew' % agent.name)
            self.metrics.register_percentage('%s_sel' % agent.name)
            self.metrics.register_uniqueness('%s_unique' % agent.name)
        # text metrics
        ref_text = ' '.join(data.read_lines(self.args.ref_text))
        self.metrics.register_ngram('full_match', text=ref_text)

    def _is_selection(self, out):
        return len(out) == 1 and out[0] == '<selection>'

    def show_metrics(self):
        return ' '.join(['%s=%s' % (k, v) for k, v in self.metrics.dict().items()])

    def get_loss(self, inpt, words, lang_hs, lang_h, c=1, bob_ends=True, bob_out=None):
        bob=self.agents[1]
        bob.read(inpt, f_encode=False)
        if bob_ends:
            loss1, bob_out, _ = bob.write_selection(wb_attack=True)
            bob_choice, classify_loss, _ = bob.choose()
            t_loss = c*loss1 + classify_loss
        else:
            #if bob_out is None:
            bob_out = bob.write(bob_ends)
            _,out,_ = self.agents[0].write_selection(wb_attack=True, alice=True)
            bob.read(out)
            bob_choice, classify_loss, _ = bob.choose()
            t_loss = classify_loss                      
        #t_loss.backward(retain_graph=True)
        bob.words = copy.copy(words)
        bob.lang_hs = copy.copy(lang_hs)
        bob.lang_h = lang_h.clone()
        if bob_ends:
            return t_loss.item(), loss1.item(), classify_loss.item(), bob_out, bob_choice
        else:
            return t_loss.item(), bob_out, bob_choice
            #return t_loss, None, bob_choice

    def attack(self, inpt, lang_hs, lang_h, words, bob_ends):
        bob = self.agents[1]


        #class_losses = []
        # generate choices for each of the agents
        #flag_loss=False
        
        c=1
        #print(words)
        all_index_n = len(self.agents[0].model.word_dict)
    
        #print(inpt)
        
        #fixed_lang_h = bob.lang_h.copy()
        fixed_ctx_h = bob.ctx_h.clone() 

        if True:
            iterations = 3
            #mask= [0] * (inpt_emb.size()[0]-1)
            for iter_idx in range(iterations):
                # projection
                min_inpt = None
                #temp_inpt = inpt.clone()
                min_loss_a = []
                min_inpt_a = []

                if bob_ends:
                    t_loss,loss1,classify_loss, bob_out, bob_choice = self.get_loss(inpt, words, lang_hs, lang_h)
                else:
                    #bob_out = bob.write(bob_ends)
                    t_loss, bob_out, bob_choice = self.get_loss(inpt, words, lang_hs, lang_h, bob_ends=bob_ends, bob_out=None)
                if bob_ends:
                    print(iter_idx,t_loss, loss1, classify_loss)
                else:
                    print(iter_idx,t_loss)
                if bob_ends:
                    if loss1==0.0 and t_loss<=-5.0:
                        print("get legimate adversarial example")
                        print(self.agents[0]._decode(inpt,bob.model.word_dict))      ### bug still?????
                        print("bob attack finished")
                    
                        break
                else:
                    if t_loss<=-3.0:
                        print("get legimate adversarial example")
                        print(self.agents[0]._decode(inpt,bob.model.word_dict))      ### bug still?????
                        print("alice attack finished")
                        break                    
                for emb_idx in range(1,inpt.size()[0]-1):                   
                    min_loss = t_loss
                    for candi in range(1,all_index_n):
                        temp_inpt = inpt.clone()
                        temp_inpt[emb_idx]=candi
                        if bob_ends:
                            loss,_,_,_,_= self.get_loss(temp_inpt, words, lang_hs, lang_h)
                        else:
                            #bob_out = bob.write(bob_ends)
                            loss,bob_out,_ = self.get_loss(temp_inpt, words, lang_hs, lang_h, bob_ends=bob_ends, bob_out=None)
                            if loss<0:
                                sum_loss=0
                                for _ in range(10):
                                    loss,_,_ = self.get_loss(temp_inpt, words, lang_hs, lang_h, bob_ends=bob_ends, bob_out=None)
                                    sum_loss += loss
                                    #print(loss)
                                loss = sum_loss/10
                        #if loss<0:
                        #    print("first loss",loss, "bob_choice", bob_choice, "bob_out", bob_out)
                            #print(temp_inpt,bob.words,bob.lang_hs,bob.lang_h.size())
                        #    print("sec loss",self.get_loss(temp_inpt, words, lang_hs, lang_h, bob_ends=bob_ends,bob_out=bob_out))
                            #print(temp_inpt,bob.words,bob.lang_hs,bob.lang_h.size())
                        #    print("third loss",self.get_loss(temp_inpt, words, lang_hs, lang_h, bob_ends=bob_ends,bob_out=bob_out))
                            #print(temp_inpt,bob.words,bob.lang_hs,bob.lang_h.size())
                        if loss<min_loss:
                            min_loss = loss
                            min_inpt = temp_inpt.clone()
                            #print(min_loss)
                            
    
    
                    min_loss_a.append(min_loss)
                    min_inpt_a.append(min_inpt)

                if len(min_loss_a)!=0:
                    min_idx_in_a = np.argmin(min_loss_a)
                    if min_inpt_a[min_idx_in_a] is not None:
                        inpt = min_inpt_a[min_idx_in_a].clone()
                    else:
                        print(min_inpt_a)
                    #print(min_inpt_a)
                    #print(min_loss_a)
                    #print(inpt)
                    #print(loss)


            
        #else:

            """
            if bob_ends:
                bob.read_emb(inpt_emb, inpt)
                _, bob_out, _ = bob.write_selection(wb_attack=True)
                bob.words = words.copy()
                bob_choice, _, _ = bob.choose(inpt_emb=inpt_emb,wb_attack=True)
            else:
                bob.read_emb(inpt_emb, inpt)
                bob_out = bob.write(bob_ends)
                out = self.agents[0].write_selection()
                bob.read(out)
                bob.words = words.copy()
                bob_choice, _, _ = bob.choose(inpt_emb=inpt_emb,bob_ends=bob_ends, bob_out=bob_out, wb_attack=True)
            """
        return bob_choice, bob_out, t_loss, inpt


    def run(self, ctxs, logger):
        """Runs one instance of the dialogue."""
        assert len(self.agents) == len(ctxs)
        # initialize agents by feeding in the contexes
        #for agent, ctx in zip(self.agents, ctxs):
        #    agent.feed_context(ctx)
        #   logger.dump_ctx(agent.name, ctx)
        self.agents[0].feed_context(ctxs[0])
        logger.dump_ctx(self.agents[0].name, ctxs[0])
        self.agents[1].feed_context(ctxs[1],ctxs[0])
        logger.dump_ctx(self.agents[1].name, ctxs[1])

        logger.dump('-' * 80)

        # choose who goes first by random
        if np.random.rand() < 0.5:
            writer, reader = self.agents
        else:
            reader, writer = self.agents

        #reader, writer = self.agents

        conv = []
        # reset metrics
        self.metrics.reset()

         #### Minhao ####
        count_turns = 0       

        #bob_ends = False
        with torch.no_grad():
            while True:
                # produce an utterance
                bob_out= None
                if count_turns > self.args.max_turns:
                    print("Failed")
                    out = writer.write_selection()
                    logger.dump_sent(writer.name, out)
                    break
                if writer == self.agents[0]:
                    inpt, lang_hs, lang_h, words = writer.write_white(reader)
                    if inpt.size()[0]>3:
                        print("try to let bob select")
                        bob_ends = True
                        bob_choice, bob_out, loss, inpt = self.attack(inpt, lang_hs, lang_h, words, bob_ends)
                        #continue
                        if loss<=-5.0 and self._is_selection(bob_out):
                            break
                        else:
                            print("try to let alice select")
                            bob_ends=False
                            inpt, lang_hs, lang_h, words = writer.write_white(reader)
                            bob_choice, bob_out, loss, inpt = self.attack(inpt, lang_hs, lang_h, words, bob_ends)
                            if loss<=-2.0:
                                break
                            else:
                                print("enlong the dialogue")
                                out = writer.write()
                                #if count_turns>3:
                                #    print("using RL sentence")
                                #    out = writer.write_rl()
                                #print(out)
                    else:
                        out = writer.write()
                else:
                    out = writer.write()

                self.metrics.record('sent_len', len(out))
                self.metrics.record('full_match', out)
                self.metrics.record('%s_unique' % writer.name, out)

                # append the utterance to the conversation
                conv.append(out)
                # make the other agent to read it
                reader.read(out)
                if not writer.human:
                    logger.dump_sent(writer.name, out)
                # check if the end of the conversation was generated
                print(out)
                if self._is_selection(out):
                    self.metrics.record('%s_sel' % writer.name, 1)
                    self.metrics.record('%s_sel' % reader.name, 0)
                    break
                writer, reader = reader, writer
                count_turns += 1
            ##### add selection mark if exceeding the max_turns

        ### Minhao: need to design loss focusing on the choices
        ### No evalution in the conversation????
        #bob_ends = False
        #bob_choice, bob_out = self.attack(inpt, lang_hs, lang_h, words, bob_ends)
        bob = self.agents[1]

        if bob_out is not None:
            if bob_ends:
                logger.dump_sent(self.agents[0].name,self.agents[0]._decode(inpt,self.agents[0].model.word_dict))
                logger.dump_sent(bob.name, bob_out)
            else:
                logger.dump_sent(self.agents[0].name, self.agents[0]._decode(inpt,self.agents[0].model.word_dict))
                logger.dump_sent(bob.name, bob._decode(bob_out, bob.model.word_dict))
                logger.dump_sent(self.agents[0].name, ['<selection>'])
        else:
            bob_choice, _, _ = bob.choose()
        #####
        choices = []
        choices.append(bob_choice)
        #print(choices)
        alice_choice = bob_choice[:]
        for indx in range(3):
           alice_choice[indx+3], alice_choice[indx] = alice_choice[indx], alice_choice[indx+3]
        choices.append(alice_choice) ######## always agree
        choices[1], choices[0] = choices[0], choices[1]
        #print(choices)
        #for agent in self.agents:
        #    choice, class_loss = agent.choose(flag=flag_loss)
        #    class_losses.append(class_loss)
        #    choices.append(choice)
        #    logger.dump_choice(agent.name, choice[: self.domain.selection_length() // 2])
        #    flag_loss=True

        # evaluate the choices, produce agreement and a reward
        #print(choices,ctxs)
        agree, rewards = self.domain.score_choices(choices, ctxs)
        logger.dump('-' * 80)
        logger.dump_agreement(agree)
        #print(rewards)
        # perform update, in case if any of the agents is learnable
        # let the difference become new reward
        ## how to combine the loss to the reward

        '''
        diff = rewards[0]-rewards[1] 
        flag = True
        agree = 1
        #print(5 - classify_loss.item())
        for agent, reward in zip(self.agents, rewards):           
            if flag:
                logger.dump_reward(agent.name, agree, reward)
                #agent.update(agree, 50-class_losses[1].item())
                agent.update(agree, 5-classify_loss.item())
                #agent.update(agree, diff - 0.05 * class_losses[1].data[0])
                #agent.update(agree, diff)
            else:
                logger.dump_reward(agent.name, agree, reward)
                if not self.args.fixed_bob:
                    agent.update(agree, reward)
            flag=False
        '''
        agree = 1
        for agent, reward in zip(self.agents, rewards):
            logger.dump_reward(agent.name, agree, reward)
            logging.debug("%s : %s : %s" % (str(agent.name), str(agree), str(rewards)))
            #agent.update(agree, 5-classify_loss.item())


        if agree:
            self.metrics.record('advantage', rewards[0] - rewards[1])
        self.metrics.record('time')
        self.metrics.record('dialog_len', len(conv))
        self.metrics.record('agree', int(agree))
        self.metrics.record('comb_rew', np.sum(rewards) if agree else 0)
        for agent, reward in zip(self.agents, rewards):
            self.metrics.record('%s_rew' % agent.name, reward if agree else 0)

        logger.dump('-' * 80)
        logger.dump(self.show_metrics())
        logger.dump('-' * 80)
        for ctx, choice in zip(ctxs, choices):
            logger.dump('debug: %s %s' % (' '.join(ctx), ' '.join(choice)))

        return conv, agree, rewards
