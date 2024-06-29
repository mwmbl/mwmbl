from numpy.random import RandomState
from sklearn.base import BaseEstimator


random = RandomState(1)


class RandomRegressor(BaseEstimator):
    def fit(self, X, y):
        pass

    def predict(self, X):
        return random.random(len(X))
