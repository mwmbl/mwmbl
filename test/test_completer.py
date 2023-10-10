import pandas as pd
from mwmbl import tinysearchengine
from mwmbl.tinysearchengine.completer import Completer


def mockCompleterData(mocker, data):
    testDataFrame = pd.DataFrame(data, columns=['','term','count'])
    mocker.patch('mwmbl.tinysearchengine.completer.Completer.get_terms', 
                 return_value = testDataFrame)


def test_correctCompletions(mocker):
    # Mock completer with custom data
    testdata = [
        [0, 'build', 4],
        [1, 'builder', 3],
        [2, 'announce', 2],
        [3, 'buildings', 1]]
    mockCompleterData(mocker, testdata)
    
    completer = Completer()
    completion = completer.complete('build')
    assert ['build', 'builder', 'buildings'] == completion


def test_correctSortOrder(mocker):
    # Mock completer with custom data
    testdata = [
        [0, 'build', 4],
        [1, 'builder', 1],
        [2, 'announce', 2],
        [3, 'buildings', 3]]
    mockCompleterData(mocker, testdata)
    
    completer = Completer()
    completion = completer.complete('build')
    assert ['build', 'buildings', 'builder'] == completion


def test_noCompletions(mocker):
    # Mock completer with custom data
    testdata = [
        [0, 'build', 4],
        [1, 'builder', 3],
        [2, 'announce', 2],
        [3, 'buildings', 1]]
    mockCompleterData(mocker, testdata)
    
    completer = Completer()
    completion = completer.complete('test')
    assert [] == completion


def test_singleCompletions(mocker):
    # Mock completer with custom data
    testdata = [
        [0, 'build', 4],
        [1, 'builder', 3],
        [2, 'announce', 2],
        [3, 'buildings', 1]]
    mockCompleterData(mocker, testdata)
    
    completer = Completer()
    completion = completer.complete('announce')
    assert ['announce'] == completion


def test_idempotencyWithSameScoreCompletions(mocker):
    # Mock completer with custom data
    testdata = [
        [0, 'build', 1],
        [1, 'builder', 1],
        [2, 'announce', 1],
        [3, 'buildings', 1]]
    mockCompleterData(mocker, testdata)
    
    completer = Completer()
    for i in range(3):
        print(f"iteration: {i}")
        completion = completer.complete('build')
        # Results expected in reverse order
        expected = ['buildings','builder','build']
        assert expected == completion
    