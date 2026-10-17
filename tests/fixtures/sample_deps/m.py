import requests
import yaml
import numpy as np
from sklearn import tree


def fetch(url):
    return requests.get(url, timeout=5)


def dump(data):
    return yaml.safe_dump(data)
