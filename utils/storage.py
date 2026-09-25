"""Session persistence.

Primary: MongoDB (set MONGO_URI). Fallback: JSON files under data/sessions/.
The stored document is a plain dict; the live fields are the FastAPI snippet of
{status, transcript, fields} plus metadata.
"""
import json
import os
import threading

import config

_PATH_LOCK = threading.Lock()


def _safe_path(session_id):
    safe = os.path.basename(str(session_id))
    return os.path.join(config.SESSIONS_DIR, f"{safe}.json")


def _get_mongo():
    if not config.MONGO_URI:
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=3000)
        return client[config.MONGO_DB][config.MONGO_COLLECTION]
    except Exception as error:
        print(f"MongoDB unavailable, using JSON-file storage: {error}")
        return None


_COLLECTION = None


def _collection():
    global _COLLECTION
    if _COLLECTION is None:
        _COLLECTION = _get_mongo()
    return _COLLECTION


class SessionStore:
    def save(self, document):
        """Upsert by session_id."""
        if not document:
            return
        if _collection() is not None:
            try:
                _collection().replace_one(
                    {"session_id": document["session_id"]}, document, upsert=True
                )
                return
            except Exception as error:
                print(f"MongoDB save failed: {error}")
        with _PATH_LOCK:
            with open(_safe_path(document["session_id"]), "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, default=str)

    def load(self, session_id):
        if _collection() is not None:
            try:
                found = _collection().find_one({"session_id": session_id})
                if found:
                    found.pop("_id", None)
                    return found
            except Exception as error:
                print(f"MongoDB load failed: {error}")
        with _PATH_LOCK:
            path = _safe_path(session_id)
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)

    def list(self):
        if _collection() is not None:
            try:
                docs = list(_collection().find().sort("started_at", -1))
                for doc in docs:
                    doc.pop("_id", None)
                return docs
            except Exception as error:
                print(f"MongoDB list failed: {error}")
        with _PATH_LOCK:
            results = []
            for name in sorted(os.listdir(config.SESSIONS_DIR), reverse=True):
                if name.endswith(".json"):
                    path = os.path.join(config.SESSIONS_DIR, name)
                    with open(path, "r", encoding="utf-8") as handle:
                        try:
                            results.append(json.load(handle))
                        except Exception:
                            continue
            return results