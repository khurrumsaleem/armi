# Copyright 2019 TerraPower, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module contains commonly used functions relating to directories, files and path
manipulations.
"""

import importlib
import os
import pathlib
import shutil
from time import sleep

from armi import context, runLog
from armi.utils import safeCopy

DO_NOT_CLEAN_PATHS = [
    "armiruns",
    "failedruns",
    "mc2run",
    "mongoose",
    "shufflebranches",
    "snapshot",
    "tests",
]


def armiAbsPath(*pathParts):
    """Convert a list of path components to an absolute path, without drive letters if possible."""
    return os.path.abspath(os.path.join(*pathParts))


def copyOrWarn(filepathDescription, sourcePath, destinationPath):
    """Copy a file or directory, or warn if the filepath doesn't exist.

    Parameters
    ----------
    filepathDescription : str
        a description of the file and/or operation being performed.
    sourcePath : str
        Filepath to be copied.
    destinationPath : str
        Copied filepath.
    """
    try:
        if os.path.isdir(sourcePath):
            shutil.copytree(sourcePath, destinationPath, dirs_exist_ok=True)
        else:
            safeCopy(sourcePath, destinationPath)
        runLog.debug("Copied {}: {} -> {}".format(filepathDescription, sourcePath, destinationPath))
    except shutil.SameFileError:
        pass
    except Exception as e:
        runLog.warning(
            "Could not copy {} from {} to {}\nError was: {}".format(filepathDescription, sourcePath, destinationPath, e)
        )


def isFilePathNewer(path1, path2):
    """Returns true if path1 is newer than path2.

    Returns true if path1 is newer than path2, or if path1 exists and path2 does not, otherwise
    raises an IOError.
    """
    exist1 = os.path.exists(path1)
    exist2 = os.path.exists(path2)
    if exist1 and exist2:
        path1stat = os.stat(path1)
        path2stat = os.stat(path2)
        return path1stat.st_mtime > path2stat.st_mtime
    elif exist1 and not exist2:
        return True
    else:
        raise IOError("Path 1 does not exist: {}".format(path1))


def isAccessible(path):
    """Check whether user has access to a given path.

    Parameters
    ----------
    path : str
        a directory or file
    """
    return os.path.exists(path)


def separateModuleAndAttribute(pathAttr):
    """
    Return True of the specified python module, and attribute of the module exist.

    Parameters
    ----------
    pathAttr : str
        Path to a python module followed by the desired attribute.
        e.g.: `/path/to/my/thing.py:MyClass`

    Notes
    -----
    The attribute of the module could be a class, function, variable, etc.

    Raises
    ------
    ValueError:
        If there is no `:` separating the path and attr.
    """
    # rindex gives last index.
    # The last is needed because the first colon index could be mapped drives in windows.
    lastColonIndex = pathAttr.rindex(":")  # this raises a valueError
    # there should be at least 1 colon. 2 is possible due to mapped drives in windows.
    return (pathAttr[:lastColonIndex]), pathAttr[lastColonIndex + 1 :]


def importCustomPyModule(modulePath):
    """
    Dynamically import a custom module.

    Parameters
    ----------
    modulePath : str
        Path to a python module.

    Returns
    -------
    userSpecifiedModule : module
        The imported python module.
    """
    modulePath = pathlib.Path(modulePath)
    if not modulePath.exists() or not modulePath.is_file():
        raise IOError(r"Cannot import module from the given path: `{modulePath}`")
    _dir, moduleName = os.path.split(modulePath)
    moduleName = os.path.splitext(moduleName)[0]  # take off the extension
    spec = importlib.util.spec_from_file_location(moduleName, modulePath)
    userSpecifiedModule = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(userSpecifiedModule)
    return userSpecifiedModule


def moduleAndAttributeExist(pathAttr):
    """
    Return True if the specified python module, and attribute of the module exist.

    Parameters
    ----------
    pathAttr : str
        Path to a python module followed by the desired attribute.
        e.g.: `/path/to/my/thing.py:MyClass`

    Returns
    -------
    bool
        True if the specified python module, and attribute of the module exist.

    Notes
    -----
    The attribute of the module could be a class, function, variable, etc.
    """
    try:
        modulePath, moduleAttributeName = separateModuleAndAttribute(pathAttr)
    except ValueError:
        return False

    modulePath = pathlib.Path(modulePath)
    if not modulePath.is_file():
        return False

    try:
        userSpecifiedModule = importCustomPyModule(modulePath)

    # Blanket except is okay since we are checking to see if a custom import will work.
    except Exception:
        return False

    return moduleAttributeName in userSpecifiedModule.__dict__


def cleanPath(path, mpiRank=0):
    """Recursively delete a path.

    !!! Be careful with this !!! It can delete the entire cluster.

    We add copious os.path.exists checks in case an MPI set of things is trying to delete everything
    at the same time. Always check filenames for some special flag when calling this, especially
    with full permissions on the cluster. You could accidentally delete everyone's work with one
    misplaced line! This doesn't ask questions.

    Safety nets include an allow-list of paths.

    This makes use of shutil.rmtree and os.remove

    Returns
    -------
    success : bool
        True if file was deleted. False if it was not.
    """
    valid = False
    if not os.path.exists(path):
        return True

    for validPath in DO_NOT_CLEAN_PATHS:
        if validPath in path.lower():
            valid = True

    if pathlib.Path(context.APP_DATA) in pathlib.Path(path).parents:
        valid = True

    if not valid:
        raise Exception("You tried to delete {0}, but it does not seem safe to do so.".format(path))

    # delete the file/directory from only one process
    if mpiRank == context.MPI_RANK:
        if os.path.exists(path) and os.path.isdir(path):
            shutil.rmtree(path)
        elif not os.path.isdir(path):
            # it's just a file. Delete it.
            os.remove(path)

    # Potentially, wait for the deletion to finish.
    maxLoops = 6
    waitTime = 0.5
    loopCounter = 0
    while os.path.exists(path):
        loopCounter += 1
        if loopCounter > maxLoops:
            break
        sleep(waitTime)

    return not os.path.exists(path)
