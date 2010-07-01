# A module for embedding Veusz within another python program

#    Copyright (C) 2005 Jeremy S. Sanders
#    Email: Jeremy Sanders <jeremy@jeremysanders.net>
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
##############################################################################

# $Id$

"""This module allows veusz to be embedded within other Python programs.
For example:

import time
import numpy
import veusz.embed as veusz

g = veusz.Embedded('new win')
g.To( g.Add('page') )
g.To( g.Add('graph') )
g.SetData('x', numpy.arange(20))
g.SetData('y', numpy.arange(20)**2)
g.Add('xy')
g.Zoom(0.5)

time.sleep(60)
g.Close()

More than one embedded window can be opened at once
"""

import atexit
import sys
import os
import os.path
import struct
import new
import cPickle
import socket
import random
import subprocess
import time

# check remote process has this API version
API_VERSION = 1

def Bind1st(function, arg):
    """Bind the first argument of a given function to the given
    parameter."""

    def runner(*args, **args2):
        return function( arg, *args, **args2 )

    return runner

def findOnPath(cmd):
    """Find a command on the system path, or None if does not exist."""
    path = os.getenv('PATH', os.path.defpath)
    pathparts = path.split(os.path.pathsep)
    for dirname in pathparts:
        cmdtry = os.path.join(dirname, cmd)
        if os.path.isfile(cmdtry):
            return cmdtry
    return None

class Embedded(object):
    """An embedded instance of Veusz.

    This embedded instance supports all the normal veusz functions
    """

    remote = None

    def __init__(self, name = 'Veusz', copyof = None):
        """Initialse the embedded veusz window.

        name is the name of the window to show.
        This method creates a new thread to run Qt if necessary
        """

        if not Embedded.remote:
            Embedded.startRemote()

        if not copyof:
            retval = self.sendCommand( (-1, '_NewWindow',
                                         (name,), {}) )
        else:
            retval = self.sendCommand( (-1, '_NewWindowCopy',
                                         (name, copyof.winno), {}) )

        self.winno, cmds = retval

        # add methods corresponding to Veusz commands
        for name, doc in cmds:
            func = Bind1st(self.runCommand, name)
            func.__doc__ = doc    # set docstring
            func.__name__ = name  # make name match what it calls
            method = new.instancemethod(func, Embedded)
            setattr(self, name, method) # assign to self

        # check API version is same
        try:
            remotever = self._apiVersion()
        except AttributeError:
            remotever = 0
        if remotever != API_VERSION:
            raise RuntimeError("Remote Veusz instance reports version %i of"
                               " API. This embed.py supports version %i." %
                               (remotever, API_VERSION))
        # define root object
        self.Root = WidgetNode(self, 'widget', '/')

    def StartSecondView(self, name = 'Veusz'):
        """Provides a second view onto the document of this window.

        Returns an Embedded instance
        """
        return Embedded(name=name, copyof=self)

    def WaitForClose(self):
        """Wait for the window to close."""

        # this is messy, polling for closure, but cleaner than doing
        # it in the remote client
        while not self.IsClosed():
            time.sleep(0.1)

    @classmethod
    def makeSockets(cls):
        """Make socket(s) to communicate with remote process.
        Returns string to send to remote process
        """

        if ( hasattr(socket, 'AF_UNIX') and hasattr(socket, 'socketpair') ):
            # convenient interface
            cls.sockfamily = socket.AF_UNIX
            sock, socket2 = socket.socketpair(cls.sockfamily,
                                              socket.SOCK_STREAM)
            sendtext = 'unix %i\n' % socket2.fileno()
            cls.socket2 = socket2
            waitaccept = False

        else:
            # otherwise mess around with internet sockets
            # * This is required for windows, which doesn't have AF_UNIX
            # * It is required where socketpair is not supported
            cls.sockfamily = socket.AF_INET
            sock = socket.socket(cls.sockfamily, socket.SOCK_STREAM)
            sock.bind( ('localhost', 0) )
            interface, port = sock.getsockname()
            sock.listen(1)
            sendtext = 'internet %s %i\n' % (interface, port)
            waitaccept = True

        return (sock, sendtext, waitaccept)

    @classmethod
    def makeRemoteProcess(cls):
        """Try to find veusz process for remote program."""
        
        # here's where to look for embed_remote.py
        thisdir = os.path.dirname(os.path.abspath(__file__))

        # build up a list of possible command lines to start the remote veusz
        if sys.platform == 'win32':
            # windows is a special case
            # we need to run embed_remote.py under pythonw.exe, not python.exe

            # look for the python windows interpreter on path
            findpython = findOnPath('pythonw.exe')
            if not findpython:
                # if it wasn't on the path, use sys.prefix instead
                findpython = os.path.join(sys.prefix, 'pythonw.exe')

            # look for veusz executable on path
            findexe = findOnPath('veusz.exe')
            if not findexe:
                try:
                    # add the usual place as a guess :-(
                    findexe = os.path.join(os.environ['ProgramFiles'],
                                           'Veusz', 'veusz.exe')
                except KeyError:
                    pass

            # here is the list of commands to try
            possiblecommands = [
                [findpython, os.path.join(thisdir, 'embed_remote.py')],
                [findexe] ]

        else:
            # try embed_remote.py in this directory, veusz in this directory
            # or veusz on the path in order
            possiblecommands = [ [sys.executable,
                                  os.path.join(thisdir, 'embed_remote.py')],
                                 [os.path.join(thisdir, 'veusz')],
                                 [findOnPath('veusz')] ]

        # cheat and look for Veusz app for MacOS under the standard application
        # directory. I don't know how else to find it :-(
        if sys.platform == 'darwin':
            findbundle = findOnPath('Veusz.app')
            if findbundle:
                possiblecommands += [ [findbundle+'/Contents/MacOS/Veusz'] ]
            else:
                possiblecommands += [[
                    '/Applications/Veusz.app/Contents/MacOS/Veusz' ]]

        for cmd in possiblecommands:
            # only try to run commands that exist as error handling
            # does not work well when interfacing with OS (especially Windows)
            if ( None not in cmd and
                 False not in [os.path.isfile(c) for c in cmd] ):
                try:
                    # we don't use stdout below, but works around windows bug
                    # http://bugs.python.org/issue1124861
                    cls.remote = subprocess.Popen(cmd + ['--embed-remote'],
                                                  shell=False, bufsize=0,
                                                  close_fds=False,
                                                  stdin=subprocess.PIPE,
                                                  stdout=subprocess.PIPE)
                    return
                except OSError:
                    pass

        raise RuntimeError('Unable to find a veusz executable on system path')

    @classmethod
    def startRemote(cls):
        """Start remote process."""
        cls.serv_socket, sendtext, waitaccept = cls.makeSockets()

        cls.makeRemoteProcess()
        stdin = cls.remote.stdin

        # send socket number over pipe
        stdin.write( sendtext )

        # accept connection if necessary
        if waitaccept:
            cls.serv_socket, address = cls.serv_socket.accept()

        # send a secret to the remote program by secure route and
        # check it comes back
        # this is to check that no program has secretly connected
        # on our port, which isn't really useful for AF_UNIX sockets
        secret = ''.join([random.choice('ABCDEFGHUJKLMNOPQRSTUVWXYZ'
                                        'abcdefghijklmnopqrstuvwxyz'
                                        '0123456789')
                          for i in xrange(16)]) + '\n'
        stdin.write(secret)
        secretback = cls.readLenFromSocket(cls.serv_socket, len(secret))
        assert secret == secretback

        # packet length for command bytes
        cls.cmdlen = struct.calcsize('<I')
        atexit.register(cls.exitQt)

    @staticmethod
    def readLenFromSocket(socket, length):
        """Read length bytes from socket."""
        s = ''
        while len(s) < length:
            s += socket.recv(length-len(s))
        return s

    @staticmethod
    def writeToSocket(socket, data):
        count = 0
        while count < len(data):
            count += socket.send(data[count:])

    @classmethod
    def sendCommand(cls, cmd):
        """Send the command to the remote process."""

        outs = cPickle.dumps(cmd)

        cls.writeToSocket( cls.serv_socket, struct.pack('<I', len(outs)) )
        cls.writeToSocket( cls.serv_socket, outs )

        backlen = struct.unpack('<I', cls.readLenFromSocket(cls.serv_socket,
                                                            cls.cmdlen))[0]
        rets = cls.readLenFromSocket( cls.serv_socket, backlen )
        retobj = cPickle.loads(rets)
        if isinstance(retobj, Exception):
            raise retobj
        else:
            return retobj

    def runCommand(self, cmd, *args, **args2):
        """Execute the given function in the Qt thread with the arguments
        given."""
        return self.sendCommand( (self.winno, cmd, args[1:], args2) )

    @classmethod
    def exitQt(cls):
        """Exit the Qt thread."""
        cls.sendCommand( (-1, '_Quit', (), {}) )
        cls.serv_socket.shutdown(socket.SHUT_RDWR)
        cls.serv_socket.close()
        cls.serv_socket, cls.from_pipe = -1, -1

############################################################################
# Tree-based interface to Veusz widget tree below

class Node(object):
    """Represents an element in the Veusz widget-settinggroup-setting tree."""

    def __init__(self, ci, wtype, path):
        self._ci = ci
        self._type = wtype
        self._path = path

    @staticmethod
    def _makeNode(ci, path):
        """Make correct class for type of object."""
        wtype = ci.NodeType(path)
        if wtype == 'widget':
            return WidgetNode(ci, wtype, path)
        elif wtype == 'setting':
            return SettingNode(ci, wtype, path)
        else:
            return SettingGroupNode(ci, wtype, path)

    @property
    def path(self):
        """Veusz full path to node"""
        return self._path

    @property
    def type(self):
        """Type of node: 'widget', 'settinggroup', or 'setting'"""
        return self._type

    def _joinPath(self, child):
        """Return new path of child."""
        if self._path == '/':
            return '/' + child
        else:
            return self._path + '/' + child

    def __getitem__(self, key):
        """Return a child widget, settinggroup or setting."""

        if self._type != 'setting':
            try:
                return self._makeNode(self._ci, self._joinPath(key))
            except ValueError:
                pass

        raise KeyError, "%s does not have key or child '%s'" % (
            self.__class__.__name__, key)

    def __getattr__(self, attr):
        """Return a child widget, settinggroup or setting."""

        if self._type == 'setting':
            pass
        elif attr[:2] != '__':
            try:
                return self._makeNode(self._ci, self._joinPath(attr))
            except ValueError:
                pass

        raise AttributeError, "%s does not have attribute or child '%s'" % (
            self.__class__.__name__, attr)

    # boring ways to get children of nodes
    @property
    def children(self):
        """Generator to get children as Nodes."""
        for c in self._ci.NodeChildren(self._path):
            yield self._makeNode(self._ci, self._joinPath(c))
    @property
    def children_widgets(self):
        """Generator to get child widgets as Nodes."""
        for c in self._ci.NodeChildren(self._path, types='widget'):
            yield self._makeNode(self._ci, self._joinPath(c))
    @property
    def children_settings(self):
        """Generator to get child settings as Nodes."""
        for c in self._ci.NodeChildren(self._path, types='setting'):
            yield self._makeNode(self._ci, self._joinPath(c))
    @property
    def children_settinggroups(self):
        """Generator to get child settingsgroups as Nodes."""
        for c in self._ci.NodeChildren(self._path, types='settinggroup'):
            yield self._makeNode(self._ci, self._joinPath(c))

    @property
    def childnames(self):
        """Get names of children."""
        return self._ci.NodeChildren(self._path)
    @property
    def childnames_widgets(self):
        """Get names of children widgets."""
        return self._ci.NodeChildren(self._path, types='widget')
    @property
    def childnames_settings(self):
        """Get names of child settings."""
        return self._ci.NodeChildren(self._path, types='setting')
    @property
    def childnames_settinggroups(self):
        """Get names of child setting groups"""
        return self._ci.NodeChildren(self._path, types='settinggroup')

    @property
    def parent(self):
        """Return parent of node."""
        if self._path == '/':
            raise TypeError, "Cannot get parent node of root node"""
        p = self._path.split('/')[:-1]
        if p == ['']:
            newpath = '/'
        else:
            newpath = '/'.join(p)
        return self._makeNode(self._ci, newpath)

    @property
    def name(self):
        """Get name of node."""
        if self._path == '/':
            return self._path
        else:
            return self._path.split('/')[-1]

class SettingNode(Node):
    """A node which is a setting."""

    def _getVal(self):
        """The value of a setting."""
        if self._type == 'setting':
            return self._ci.Get(self._path)
        raise TypeError, "Cannot get value unless is a setting"""

    def _setVal(self, val):
        if self._type == 'setting':
            self._ci.Set(self._path, val)
        else:
            raise TypeError, "Cannot set value unless is a setting."""

    val = property(_getVal, _setVal)

class SettingGroupNode(Node):
    """A node containing a group of settings."""

    pass

class WidgetNode(Node):
    """A node pointing to a widget."""

    def WalkWidgets(self, widgettype=None):
        """Generator to walk widget tree and get widgets below this
        WidgetNode of type given.

        widgettype is a Veusz widget type name or None to get all
        widgets."""

        for widget in self.children_widgets:
            if widgettype is None or (
                self._ci.WidgetType(widget.path) == widgettype):
                yield widget
            for w in widget.WalkWidgets(widgettype=widgettype):
                yield w

    def Add(self, widgettype, *args, **args_opt):
        """Add a widget of the type given, returning the Node instance.
        """

        args_opt['widget'] = self._path
        name = self._ci.Add(widgettype, *args, **args_opt)
        return WidgetNode( self._ci, 'widget', self._joinPath(name) )

    def Rename(self, newname):
        """Renames widget to name given."""

        if self._path == '/':
            raise RuntimeError, "Cannot rename root widget"

        self._ci.Rename(self._path, newname)
        self._path = '/'.join( self._path.split('/')[:-1] + [newname] )
        
    def Action(self, action):
        """Applies action on widget."""
        self._ci.Action(action, widget=self._path)

    def Remove(self):
        """Removes a widget and its children."""
        self._ci.Remove(self._path)
