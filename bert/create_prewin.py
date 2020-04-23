import os
import sys
import codecs
from spacy.lang.en import English
import spacy
import numpy as np
import h5py

import argparse
import torch
from torch.nn.functional import pad as torch_pad
from pytorch_pretrained_bert import BertTokenizer, BertModel, BertForMaskedLM, BertForTokenClassification

path = sys.argv[1]
# path = "~/metonymy-resolution/harvest-data/disambiguation-pages/corpora/new-corpora/prewin-multi/wiki_LOCATION_train.txt"  # Input file name.
dirname = os.path.dirname(path)
name = os.path.basename(path)
rawname = os.path.splitext(name)[0] # without extension

if 'lit' in name or 'literal' in name or 'LOCATION' in name:
    label = 0
else:
    if 'met' in name or 'metonymic' in name or 'mixed' in name:
        label = 1 # 1 is for METONYMY/NON-LITERAL, 0 is for LITERAL
    elif 'INSTITUTE' in name:
        label = 1
    elif 'TEAM' in name:
        label = 2
    elif 'ARTIFACT' in name:
        label = 3
    elif 'EVENT' in name:
        label = 4

bert_version = 'bert-base-uncased'
model = BertModel.from_pretrained(bert_version)
model.eval()
spacy_tokenizer = English(parser=False)
bert_tokenizer = BertTokenizer.from_pretrained(bert_version)
en_nlp = spacy.load('en')
inp = codecs.open(path, mode="r", encoding="utf-8")
# PLEASE FORMAT THE INPUT FILE AS ONE SENTENCE PER LINE. SEE BELOW:
# ENTITY<SEP>sentence<ENT>ENTITY<ENT>rest of sentence.
# Germany<SEP>Their privileges as permanent Security Council members, especially the right of veto, 
# had been increasingly questioned by <ENT>Germany<ENT> and Japan which, as major economic powers.
out = []
seq_length = 5  # A window of 5 is the DEFAULT for the PUBLICATION methodology. Feel free to experiment.


def locate_entity(document, ent, left_w, right_w):
    left_w = '' if len(left_w) == 0 else left_w[-1].text
    right_w = '' if len(right_w) == 0 else right_w[0].text
    for doc in document:
        if doc.text == ent[0]:
            index = doc.i
            if left_w == '' or document[index - 1].text == left_w:
                if right_w == '' or document[index + len(ent)].text == right_w:
                    return index + len(ent) - 1
    raise Exception()  # If this is ever triggered, there are problems parsing the text. Check SpaCy output!


def find_start(old):
    while old.dep_ == "conj":
        old = old.head
    return old.head


def pad(coll, from_left):
    while len(coll) < seq_length:
        if from_left:
            coll = [u"0.0"] + coll
        else:
            coll = coll + [u"0.0"]
    return coll


def bert_pad(coll, from_left):
    '''
        Perform zero padding
    '''
    while len(coll) < seq_length:
        if from_left:
            coll = torch_pad(coll, [0, 0, 1, 0], mode='constant', value=0)
        else:
            coll = torch_pad(coll, [0, 0, 0, 1], mode='constant', value=0)
    return coll

def dump_to_hdf5(file, out):
    dep_lefts, bert_lefts, dep_rights, bert_rights, labels = [], [], [], [], []
    for row in out:
        dep_lefts.append(row[1])
        bert_lefts.append(row[2].numpy())
        dep_rights.append(row[4])
        bert_rights.append(row[5].numpy())
        labels.append(row[6])

    '''
        SpaCy dependency tags are string encoded in unicode (Python 3 type: str).

        Read the section linked below for info on writing encoded string.
        http://docs.h5py.org/en/latest/strings.html#exceptions-for-python-3
    '''
    dep_lefts = np.array(dep_lefts, dtype='S10')
    dep_rights = np.array(dep_rights, dtype='S10')

    with h5py.File(file, 'w') as f:
        dt = h5py.string_dtype(encoding='utf-8')
        f.create_dataset('dep_lefts', data=dep_lefts, dtype=dt, compression='gzip', compression_opts=9)
        f.create_dataset('bert_lefts', data=bert_lefts, dtype=np.float32, compression='gzip', compression_opts=9)
        f.create_dataset('dep_rights', data=dep_rights, dtype=dt, compression='gzip', compression_opts=9)
        f.create_dataset('bert_rights', data=bert_rights, dtype=np.float32, compression='gzip', compression_opts=9)
        f.create_dataset('labels', data=labels, dtype=np.uint8, compression='gzip', compression_opts=9)

    return

for line in inp:
    line = line.split(u"<SEP>")
    sentence = line[1].split(u"<ENT>")
    entity = [t.text for t in spacy_tokenizer(sentence[1])]
    en_doc = en_nlp(u"".join(sentence).strip())
    words = []
    index = locate_entity(en_doc, entity, spacy_tokenizer(sentence[0].strip()), spacy_tokenizer(sentence[2].strip()))
    start = find_start(en_doc[index])

    # --------------------------------------------------------------------
    # Token map will be an int -> int mapping
    #    between the `spacy_tokens` index and the `bert_tokens` index.
    spacy_to_bert_map = []
    bert_tokens = []
    spacy_tokens = [token.text for token in en_doc]

    '''
        According to https://mccormickml.com/2019/05/14/BERT-word-embeddings-tutorial/
            [CLS] amd [SEP] tokens are important.
        Also, use the segment_ids to inform BERT
            that the input is just one sentence.
    '''
    spacy_tokens = ["[CLS]"] + spacy_tokens + ["[SEP]"]

    for orig_token in spacy_tokens:
        spacy_to_bert_map.append(len(bert_tokens))
        bert_tokens.extend(bert_tokenizer.tokenize(orig_token))

    segments_ids = [1] * len(bert_tokens)

    try:
        token_ids = bert_tokenizer.convert_tokens_to_ids(bert_tokens)
        tokens_tensor = torch.tensor([token_ids])
        segments_tensors = torch.tensor([segments_ids])
        with torch.no_grad():
            encoded_layers, _ = model(tokens_tensor, segments_tensors, output_all_encoded_layers=True)

        '''
            According to http://jalammar.github.io/illustrated-bert/
                concatenating the last four hidden four layers
                is a good choice as a contextualised ELMo-like word embeddings.

            Concatenation leads to very long tensors.
            So I decided to take sum of the last four hiddden layers.
            This is the second best approach according to the blog.
        '''
        bert_emb = torch.add(encoded_layers[-1],
                             encoded_layers[-2]).add(encoded_layers[-3]).add(encoded_layers[-4]).squeeze()
        bert_emb_length = bert_emb.shape[-1]

        '''
            Perform summation of subword embeddings to compute word embeddings
            Another choice is to compute the average of the subword embeddings.
            Concatenation is obviously not a good choice here.
            Source: https://mccormickml.com/2019/05/14/BERT-word-embeddings-tutorial/

            Here, we perform summation of subword embeddings.
        '''
        cond_bert_emb = torch.zeros(len(spacy_tokens), bert_emb_length)
        for spacy_index in range(len(spacy_tokens)):
            start_bert_index = spacy_to_bert_map[spacy_index]
            try:
                end_bert_index = spacy_to_bert_map[spacy_index + 1]
            except IndexError:
                end_bert_index = len(bert_tokens)
            for foo in range(start_bert_index, end_bert_index):
                cond_bert_emb[spacy_index] = cond_bert_emb[spacy_index].add(bert_emb[foo])
    except ValueError:
        cond_bert_emb = torch.zeros(len(spacy_tokens), 768)
        print('ValueError Exception caught!')

    '''
        Since the two special tokens are added,
            strip bert embeddings appropriately.
        Now bert embeddings are in sync in spacy parse.
    '''
    cond_bert_emb = cond_bert_emb[1:-1]
    assert (len(cond_bert_emb) == len(en_doc))
    # --------------------------------------------------------------------

    left = seq_length * ["0.0"]
    right = seq_length * ["0.0"]
    dep_left = seq_length * ["0.0"]
    dep_right = seq_length * ["0.0"]
    bert_left = torch.zeros((seq_length, bert_emb_length))
    bert_right = torch.zeros_like(bert_left)

    if start.i > index:
        if index + 1 < len(en_doc) and en_doc[index + 1].dep_ in [u"case", u"compound", u"amod"] \
                and en_doc[index + 1].head == en_doc[index]:  # any neighbouring word that links to it
            right = pad([en_doc[index + 1].text] + [t.text for t in en_doc[start.i:][:seq_length - 1]], False)
            dep_right = pad([en_doc[index + 1].dep_] + [t.dep_ for t in en_doc[start.i:]][:seq_length - 1], False)
            bert_right = bert_pad(torch.cat((torch.unsqueeze(cond_bert_emb[index + 1], 0), cond_bert_emb[start.i:][:seq_length - 1])), False)
        else:
            right = pad([t.text for t in en_doc[start.i:][:seq_length]], False)
            dep_right = pad([t.dep_ for t in en_doc[start.i:]][:seq_length], False)
            bert_right = bert_pad(cond_bert_emb[start.i:][:seq_length], False)
    else:
        if index - len(entity) >= 0 and en_doc[index - len(entity)].dep_ in [u"case", u"compound", u"amod"] \
                and en_doc[index - len(entity)].head == en_doc[index]:  # any neighbouring word that links to it
            left = pad([t.text for t in en_doc[:start.i + 1][-(seq_length - 1):]] + [en_doc[index - len(entity)].text], True)
            dep_left = pad([t.dep_ for t in en_doc[:start.i + 1]][-(seq_length - 1):] + [en_doc[index - len(entity)].dep_], True)
            bert_left = bert_pad(torch.cat((cond_bert_emb[:start.i + 1][-(seq_length - 1):], torch.unsqueeze(cond_bert_emb[index - len(entity)], 0))), True)
        else:
            left = pad([t.text for t in en_doc[:start.i + 1][-seq_length:]], True)
            dep_left = pad([t.dep_ for t in en_doc[:start.i + 1]][-seq_length:], True)
            bert_left = bert_pad(cond_bert_emb[:start.i + 1][-seq_length:], True)
    assert(bert_left.shape == bert_right.shape)
    assert(len(left) == len(dep_left) == len(bert_left))
    assert(len(right) == len(dep_right) == len(bert_right))
    out.append((left, dep_left, bert_left, right, dep_right, bert_right, label))
    #print(left, right)
    #print(dep_left, dep_right)
    #print(bert_left, bert_right)
    #print(label)
    #print(line[1])
print("Processed:{} lines/sentences.".format(len(out)))
dump_to_hdf5("{}/bert_pickle/{}.hdf5".format(dirname, rawname), out)