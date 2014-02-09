from __future__ import division
from threading import Timer, Lock, Thread
import time
from collections import defaultdict
import socket
from xml.etree.ElementTree import XML
from itertools import izip_longest
import struct
import numpy as np
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy import Column, Integer, String, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.exc import *
from sqlalchemy.orm.exc import *
import struct, random
import zmq
from multiprocessing import Process

CMOS_ID = 1310720
BASE_ID = 1048576

TZERO = datetime.today()

Base = declarative_base()

class CMOSCount(Base):
    __tablename__ = 'cmos_count'

    index = Column(Integer, primary_key=True)
    value = Column(Integer)
    timestamp = Column(Float)

    def __init__(self, index, value, timestamp):
        self.index = index
        self.value = value
        self.timestamp = timestamp

    def __repr__(self):
        return "<CMOSCount(index=%i,value=%i)>" % (self.index, self.value) 

class CMOSRate(Base):
    __tablename__ = 'cmos_rate'

    index = Column(Integer, primary_key=True)
    value = Column(Integer)
    timestamp = Column(Float)

    def __init__(self, index, value, timestamp):
        self.index = index
        self.value = value
        self.timestamp = timestamp

    def __repr__(self):
        return "<CMOSRate(index=%i,value=%i)>" % (self.index, self.value) 


def grouper(iterable, n, fillvalue=None):
    "Collect data into fixed-length chunks or blocks"
    # grouper('ABCDEFG', 3, 'x') -> ABC DEF Gxx
    args = [iter(iterable)]*n
    return izip_longest(fillvalue=fillvalue, *args)

def parse_cmos(rec):
    crate, slot_mask = struct.unpack('II', rec[:8])
    channel_mask = np.frombuffer(rec[8:8+4*16], dtype=np.uint32)
    delay, error_flags = struct.unpack('II',rec[72:72+2*4])
    counts = np.frombuffer(rec[80:80+8*32*4], dtype=np.uint32).reshape((8,-1))
    date_string = rec[21*4+8*32*4-4:].strip('\x00')
    timestamp = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%fZ')
    return crate, slot_mask, channel_mask, delay, error_flags, counts, timestamp

def parse_base(rec):
    crate, slot_mask = struct.unpack('II', rec[:8])
    channel_mask = np.frombuffer(rec[8:8+4*16], dtype=np.uint32)
    error_flags = struct.unpack('I',rec[72:72+4])
    counts = np.frombuffer(rec[76:76+16*32], dtype=np.uint8).reshape((16,-1))
    busy = np.frombuffer(rec[76+16*32:76+16*32+16*32], dtype=np.uint8).reshape((16,-1))
    date_string = rec[76+2*16*32:].strip('\x00')
    timestamp = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%fZ')
    return crate, slot_mask, channel_mask, error_flags, counts, busy, timestamp

def parse_header(header):
    root = XML(header)
    assert root.tag == 'plist'

    def parse_item(item):
        if item.tag == 'integer':
            return int(item.text)
        elif item.tag == 'string':
            return item.text
        elif item.tag == 'dict':
            d = {}
            for key, value in grouper(item,2):
                d[key.text] = parse_item(value)
            return d
        elif item.tag == 'array':
            return [parse_item(x) for x in item]
        elif item.tag == 'false':
            return False
        elif item.tag == 'true':
            return True
        elif item.tag == 'real':
            return float(item.text)
        else:
            raise Exception("can't parse %s" % item.tag)

    return [parse_item(x) for x in root]

def total_seconds(td):
    """Returns the total number of seconds contained in the duration."""
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6

def orca_producer(hostname='snoplusdaq1', port=44666):
    socket = Socket()
    socket.connect(hostname, port)

    push_context = zmq.Context()
    push = push_context.socket(zmq.PUSH)
    push.bind('tcp://127.0.0.1:5557')

    while True:
        push.send_pyobj(socket.recv_record())

def orca_consumer():
    pull_context = zmq.Context()
    pull = pull_context.socket(zmq.PULL)
    pull.connect('tcp://127.0.0.1:5557')

    engine = create_engine('sqlite:///:memory:', echo=False)
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine,autoflush=False,autocommit=False))

    while True:
        session = Session()

        id, rec = pull.recv_pyobj()

        if id == CMOS_ID:
            crate, slotmask, channelmask, delay, error_flags, counts, timestamp = \
                parse_cmos(rec)

            for i, slot in enumerate(i for i in range(16) if (slotmask >> i) & 1):
                for j, value in enumerate(map(int,counts[i])):
                    if not channelmask[slot] & (1 << j) or value >> 31:
                        continue

                    index = crate << 16 | slot << 8 | j

                    t = total_seconds(timestamp-TZERO)

                    try:
                        count = session.query(CMOSCount).filter(CMOSCount.index == index).one()

                        rate = (value-count.value)/(t-count.timestamp)
                        cmos_rate = CMOSRate(index,rate,t)
                        session.add(cmos_rate)
                    except NoResultFound:
                        count = CMOSCount(index,value,t)
                        session.add(count)
                    else:
                        count.value = value
                        count.timestamp = t

                    session.commit()

class CMOSThread(Thread):
    def __init__(self, callback, hostname='snoplusdaq1', port=44666):
        Thread.__init__(self)
        self.callback = callback
        self.socket = Socket()
        self.hostname = hostname
        self.port = port

        self.cmos = {}
        self.tzero = datetime.now()

    def reconnect(self):
        import time
        self.socket.close()
        time.sleep(1.0)
        self.socket.connect(self.hostname, self.port)

    def run(self):
        self.socket.connect(self.hostname, self.port)

        # receive header
        id, rec = self.socket.recv_record()

        assert id == 0
        header = parse_header(rec[4:-1])

        datadesc = header[0]['dataDescription']

        cmos_id = datadesc['ORXL3Model']['Xl3CmosRate']['dataId']
        base_id = datadesc['ORXL3Model']['Xl3PmtBaseCurrent']['dataId']

        while True:
            try:
                id, rec = self.socket.recv_record()
            except Exception as e:
                print e
                print 'reconnecting'
                try:
                    self.reconnect()
                except Exception as e2:
                    print e2
                    pass
                continue

            if id == base_id:
                crate, slotmask, channelmask, error_flags, counts, busy, timestamp = \
                    parse_base(rec)

                adc = {}

                for i, slot in enumerate(i for i in range(16) if (slotmask >> i) & 1):
                    adc[slot] = {}
                    for j, count in enumerate(counts[i]):
                        if not channelmask[slot] & (1 << j) or count >> 31:
                            continue

                        adc[slot][j] = int(count) - 127

                self.callback({'key': 'base', 'crate': crate, 'adc': adc})

            if id == cmos_id:
                crate, slotmask, channelmask, delay, error_flags, counts, timestamp = \
                    parse_cmos(rec)

                rate = {}

                if crate not in self.cmos:
                    self.cmos[crate] = {}

                for i, slot in enumerate(i for i in range(16) if (slotmask >> i) & 1):
                    if slot not in self.cmos[crate]:
                        self.cmos[crate][slot] = {}

                    rate[slot] = {}

                    for j, count in enumerate(counts[i]):
                        if not channelmask[slot] & (1 << j) or count >> 31:
                            continue

                        if j in self.cmos[crate][slot]:
                            prev_count, prev_time = self.cmos[crate][slot][j]
                            dt = total_seconds(timestamp-prev_time)
                            if dt < 10.0:
                                rate[slot][j] = (count-prev_count)/dt

                        self.cmos[crate][slot][j] = count, timestamp

                self.callback({'key': 'cmos', 'crate': crate, 'rate': rate})

class Socket(object):
    def __init__(self, sock=None):
        if sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        else:
            self.sock = sock

    def connect(self, host, port):
        self.sock.connect((host,port))

    def is_short(self, datarecord):
        return (datarecord & 0x80000000) == 0x80000000

    def get_dataid(self, datarecord):
        if self.is_short(datarecord):
            return datarecord & 0xfc000000
        else:
            return datarecord & 0xfffc0000

    def get_length(self, datarecord):
        if self.is_short(datarecord):
            return 1
        else:
            return datarecord & 0x3ffff

    def send(self, msg):
        totalsent = 0
        while totalsent < len(msg):
            sent = self.sock.send(msg[totalsent:])
            if sent == 0:
                raise RuntimeError('socket connection broken')

            totalsent += sent

    def recv(self, size):
        msg = ''
        while len(msg) < size:
            chunk = self.sock.recv(size-len(msg))
            if chunk == '':
                raise RuntimeError('socket connection broken')
            msg += chunk
        return msg

    def recv_record(self):
        rec, = struct.unpack('I',self.recv(4))

        if self.is_short(rec):
            return self.get_dataid(rec), rec & 0x3ffffff
        else:
            # -1 because data record counts
            size = self.get_length(rec)*4 - 4

            return self.get_dataid(rec), self.recv(size)

expire = 60

cmos = lambda: None # just a mock object
cmos.lock = Lock()
cmos.items = []
cmos.now = {}
cmos.max = {}
cmos.avg = {}

base = lambda: None # just a mock object
base.lock = Lock()
base.items = []
base.now = {}
base.avg = {}
base.max = {}

def update(obj):
    with obj.lock:
        now = time.time()
        obj.items = filter(lambda x: x[2] > now - expire, obj.items)

    group = defaultdict(list)

    for k, v, t in obj.items:
        group[k].append(v)

    obj.max = dict((k, max(v)) for k, v in group.iteritems())
    obj.avg = dict((k, sum(v)/len(v)) for k, v in group.iteritems())
    obj.now = dict((k, v[-1]) for k, v in group.iteritems())

def callback(item):
    key = item['key']
    crate = item['crate']

    if key == 'cmos':
        with cmos.lock:
            for j, card in item['rate'].iteritems():
                for k, rate in card.iteritems():
                    index = (crate << 16) | (j << 8) | k
                    cmos.items.append((index,rate,time.time()))
        update(cmos)
    elif key == 'base':
        with base.lock:
            for j, card in item['adc'].iteritems():
                for k, adc in card.iteritems():
                    index = (crate << 16) | (j << 8) | k
                    base.items.append((index,adc,time.time()))
        update(base)

# p = Process(target=orca_producer)
# p.start()
# 
# p2 = Process(target=orca_consumer)
# p2.start()

c = CMOSThread(callback)
c.start()
