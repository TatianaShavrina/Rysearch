import numpy as np
import pandas as pd
import pickle
import json
import zmq
import re
import os

from sklearn.metrics.pairwise import pairwise_distances
from pymongo import MongoClient
from scipy.linalg import norm

import artm
from experiments import hierarchy_utils

from parsers import arbitrary, text_utils

MODEL_PATH = "hartm"
TRANSFORM_PATH = "uploads/transform.txt"
BATCH_PATH = "uploads/transform_batches/"

ZMQ_BACKEND_PORT = 2511

EMPTY, UP = b"", b"UP"

EDGE_THRESHOLD = 0.05
DOC_THRESHOLD = 0.25
TOP_N_WORDS = 3
TOP_N_REC_DOCS = 5
TOP_N_TOPIC_DOCS = 20

def hellinger_dist(p, q):
    return norm(np.sqrt(p) - np.sqrt(q))

def from_artm_tid(artm_tid):
    # This is due to hARTM bug
    if artm_tid.startswith("level_0_"):
        return (0, artm_tid[8:])
    else:
        lid, tid = artm_tid[5:].split("_", 1)
        lid = int(lid)
        return (lid, tid)

def to_artm_tid(lid, tid):
    if lid < 0 or lid > len(Ts) or tid < 0 or tid >= sum(Ts):
        return None

    # This is due to hARTM bug
    if lid == 0:
        return "level_%d_topic_%d" % (lid, tid)
    else:
        return "level%d_topic_%d" % (lid, tid)

def get_documents_by_ids(docs_ids, with_texts=True, with_modalities=False):
    fields = {"title": 1, "authors_names" : 1}
    prefix_to_col_map = {"pn": "postnauka", "habr": "habrahabr"}
    if with_texts:
        fields["markdown"] = 1
    if with_modalities:
        fields["modalities"] = 1
    queries = {}
    for doc_id in docs_ids:
        prefix = doc_id.split("_", 1)[0]
        col_name = prefix_to_col_map[prefix]
        if col_name not in queries:
            queries[col_name] = []
        queries[col_name].append(doc_id)
    result = []
    for col_name, col_docs_ids in queries.items():
        result += db[col_name].find({"_id": {"$in": col_docs_ids}}, fields)
    result_map = dict(map(lambda v: (v["_id"], v), result))
    response = []
    for doc_id in docs_ids:
        if doc_id not in result_map:
            continue
        doc = result_map[doc_id]
        res = {
            "doc_id":        doc["_id"],
            "title":         doc["title"],
            "authors_names": doc["authors_names"],
        }
        if with_texts:
            res["markdown"] = doc["markdown"]
        if with_modalities:
            res["modalities"] = doc["modalities"]
        response.append(res)
    return response

# Initialize MongoDB
mongo_client = MongoClient()
db = mongo_client["datasets"]

# Initialize ARTM data
artm_extra_info = pickle.load(open(MODEL_PATH + "/extra_info.dump", "rb"))
artm_model = hierarchy_utils.hARTM(theta_columns_naming="title",
                                   cache_theta=True,
                                   class_ids=artm_extra_info["class_ids"])
artm_model.load(MODEL_PATH)

# Extract Phi, Psi and Theta matrices
phis = []
psis = []
theta = artm_extra_info["theta"]
for level_idx, artm_level in enumerate(artm_model._levels):
    phis.append(artm_level.get_phi(class_ids="flat_tag"))
    if level_idx > 0:
        psis.append(artm_level.get_psi())

topics = {}
T = lambda lid, tid: "level_%d_%s" % (lid, tid)
unT = lambda t: list(map(int, t[6:].split("_topic_")))

# Change this constants if model changes
Ts = [20, 77]
all_topics = theta.index
rec_topics = list(filter(lambda t: re.match("level1_topic_*", t), all_topics))
rec_theta = theta.T[rec_topics].sort_index()

# Create subject topic names
for lid, phi in enumerate(phis):
    names = phi.index[phi.values.argsort(axis=0)[-TOP_N_WORDS:][::-1].T]
    for tid, top_words in zip(phi.columns, names):
        # subject topic names are "topic_X", where X = 0, 1, ...
        # background topic names are "background_X", where X = 0, 1, ...
        if re.match("^topic_\d+$", tid):
            topics[T(lid, tid)] = {
                "level_id":  lid,
                "top_words": list(top_words),
                "parents":   [],
                "children":  [],
            }

# Collect topic edges
for lid, psi in enumerate(psis):
    density = 0
    psi = psi > EDGE_THRESHOLD
    for tid1 in psi.columns:
        if re.match("^topic_\d+$", tid1):
            for tid2 in psi.index:
                if re.match("^topic_\d+$", tid2) and psi.loc[tid2, tid1]:
                    density += 1
                    topics[T(lid, tid1)]["children"].append(T(lid + 1, tid2))
                    topics[T(lid + 1, tid2)]["parents"].append(T(lid, tid1))

# Initialize doc thresholds
doc_topics = list(filter(lambda t: re.match("level1_topic_*", t), all_topics))
doc_theta = theta.loc[doc_topics]
doc_thresholds = doc_theta.max(axis=0) / np.sqrt(2)

# Assign integer weights to topics
topic_docs_count = doc_theta.apply(lambda s: sum(s >= doc_thresholds), axis=1)
for artm_tid in doc_topics:
    topic_id = T(*from_artm_tid(artm_tid))
    w = int(topic_docs_count[artm_tid])
    topics[topic_id]["weight"] = w
for topic_id in topics:
    if "weight" not in topics[topic_id]:
        # TODO: fix when we have number of levels > 2
        topics[topic_id]["weight"] = 0
        for child_topic_id in topics[topic_id]["children"]:
            topics[topic_id]["weight"] += topics[child_topic_id]["weight"]

def process_msg(message):
    if message["act"] == "get_topics":
        response = topics
    elif message["act"] == "get_documents":
        lid, tid = unT(message["topic_id"])
        artm_tid = to_artm_tid(lid, tid)
        if artm_tid is None:
            response = "Incorrect `topic_id`"
        else:
            ptd = doc_theta.loc[artm_tid]
            sorted_ptd = ptd[ptd >= doc_thresholds].sort_values()
            sorted_ptd = sorted_ptd[-TOP_N_TOPIC_DOCS:][::-1]
            docs_ids = sorted_ptd.index
            docs = get_documents_by_ids(docs_ids, with_texts=False)
            weights = {k: float(v) for k, v in sorted_ptd.items()}
            response = {"docs": docs, "weights": weights}
    elif message["act"] == "get_document":
        docs_ids = [message["doc_id"]]
        docs = get_documents_by_ids(docs_ids, with_modalities=True)
        response = docs[0] if len(docs) > 0 else None
    elif message["act"] == "get_recommendations":
        doc_id = message["doc_id"]
        if doc_id not in rec_theta.index:
            response = "Unknown `doc_id`"
        else:
            doc = rec_theta.loc[doc_id]
            dist = pairwise_distances([doc], rec_theta, hellinger_dist)[0]
            dist_series = pd.Series(data=dist, index=rec_theta.index)
            sim_docs_ids = dist_series.sort_values().index
            sim_docs_ids = sim_docs_ids[1:TOP_N_REC_DOCS + 1]
            response = get_documents_by_ids(sim_docs_ids, with_texts=False)
    elif message["act"] == "transform_doc":
        doc_path = message["doc_path"]
        with open(doc_path) as doc_file:
            # Parse uploaded file
            doc = pipeline.fit_transform(doc_file)
            vw_file = open(TRANSFORM_PATH, "w")
            # Save to Vowpal Wabbit file
            text_utils.VowpalWabbitSink(vw_file, lambda x: "upload") \
                      .fit_transform([doc])
            # Create batch and transform it into Theta vector
            # TODO: rewrite using data_format="bow_n_wd"
            transform_batch = artm.BatchVectorizer(data_format="vowpal_wabbit",
                                                   data_path=TRANSFORM_PATH,
                                                   batch_size=1,
                                                   target_folder=BATCH_PATH)
            transform_theta = artm_model.transform(transform_batch)
            # Make a response
            response = {"theta": {}}
            for artm_tid, prob in transform_theta["upload"].items():
                topic_id = T(*from_artm_tid(artm_tid))
                response["theta"][topic_id] = float(prob)
        # Delete uploaded file
        os.remove(doc_path)
    else:
        response = "Unknown query"

    return response

try:
    # Initialize arbitrary pipeline
    pipeline = arbitrary.get_pipeline()

    # Initialize ZeroMQ
    context = zmq.Context()
    socket = context.socket(zmq.DEALER)
    # TODO: maybe set socket identity for persistence?
    socket.connect("tcp://localhost:%d" % ZMQ_BACKEND_PORT)

    # Notify ARTM_proxy that we're up
    socket.send(UP)

    print("ARTM_bridge: start serving ZeroMQ queries on port",
          ZMQ_BACKEND_PORT)

    while True:
        # Wait for next request from client
        client, request = socket.recv_multipart()
        message = json.loads(request.decode("utf-8"))

        # Debug logging
        # print("> " + json.dumps(message))

        # Process message
        response = process_msg(message)

        socket.send_multipart([
            client,
            json.dumps({
                "act":  message["act"],
                "id":   message.get("id"),
                "data": response
            }).encode("utf-8")
        ])
except Exception as e:
    print(e)
    print("Shutting down ARTM_bridge...")
finally:
    # Clean up
    socket.close()
    context.term()
