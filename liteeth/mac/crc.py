#
# This file is part of LiteEth.
#
# Copyright (c) 2015-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2017 whitequark <whitequark@whitequark.org>
# Copyright (c) 2018 Felix Held <felix-github@felixheld.de>
# SPDX-License-Identifier: BSD-2-Clause

from functools import reduce
from operator import xor
from collections import OrderedDict

from liteeth.common import *

from migen.genlib.misc import chooser, WaitTimer

# MAC CRC Engine -----------------------------------------------------------------------------------

class LiteEthMACCRCEngine(Module):
    """Cyclic Redundancy Check Engine

    Compute next CRC value from last CRC value and data input using
    an optimized asynchronous LFSR.

    Parameters
    ----------
    data_width : int
        Width of the data bus.
    width : int
        Width of the CRC.
    polynom : int
        Polynom of the CRC (ex: 0x04C11DB7 for IEEE 802.3 CRC)

    Attributes
    ----------
    data : in
        Data input.
    last : in
        last CRC value.
    next :
        next CRC value.
    """
    def __init__(self, data_width, width, polynom):
        self.data = Signal(data_width)
        self.last = Signal(width)
        self.next = Signal(width)

        # # #

        def _optimize_eq(l):
            """
            remove an even numbers of XORs with the same bit
            replace an odd number of XORs with a single XOR
            """
            d = OrderedDict()
            for e in l:
                if e in d:
                    d[e] += 1
                else:
                    d[e] = 1
            r = []
            for key, value in d.items():
                if value%2 != 0:
                    r.append(key)
            return r

        # compute and optimize the parallel implementation of the CRC's LFSR
        taps = [x for x in range(width) if (1 << x) & polynom]
        curval = [[("state", i)] for i in range(width)]
        for i in range(data_width):
            feedback = curval.pop() + [("din", i)]
            for j in range(width-1):
                if j+1 in taps:
                    curval[j] += feedback
                curval[j] = _optimize_eq(curval[j])
            curval.insert(0, feedback)

        # implement logic
        for i in range(width):
            xors = []
            for t, n in curval[i]:
                if t == "state":
                    xors += [self.last[n]]
                elif t == "din":
                    xors += [self.data[n]]
            self.comb += self.next[i].eq(reduce(xor, xors))

# MAC CRC32 ----------------------------------------------------------------------------------------

@ResetInserter()
@CEInserter()
class LiteEthMACCRC32(Module):
    """IEEE 802.3 CRC

    Implement an IEEE 802.3 CRC generator/checker.

    Parameters
    ----------
    data_width : int
        Width of the data bus.

    Attributes
    ----------
    data : in
        Data input.
    last_be : in
        Valid byte in data input (optional).
    value : out
        CRC value (used for generator).
    error : out
        CRC error (used for checker).
    """
    width   = 32
    polynom = 0x04C11DB7
    init    = 2**width-1
    check   = 0xC704DD7B
    def __init__(self, data_width):
        dw = data_width//8

        self.data  = Signal(data_width)
        self.last_be = Signal(dw)
        self.value = Signal(self.width)
        self.error = Signal()
        # Add a separate last_be signal, to maintain backwards compatability
        last_be = Signal(data_width//8)

        # # #

        self.comb += [
            If(self.last_be != 0,
                last_be.eq(self.last_be)
            ).Else(
                last_be.eq(2**(dw-1)))
        ]
        # Since the data can end at any byte end, indicated by `last_be`
        # maintain separate engines for each 8 byte increment in the data word
        engines = [LiteEthMACCRCEngine((e+1)*8, self.width, self.polynom) for e in range(dw)]
        self.submodules += engines

        regs = [Signal(self.width, reset=self.init) for e in range(dw)]
        self.sync += [regs[e].eq(engines[e].next) for e in range(dw)]
        self.comb += [engines[e].data.eq(self.data[:(e+1)*8]) for e in range(dw)],
        self.comb += [engines[e].last.eq(regs[-1]) for e in range(dw)]
        self.comb += [
                If(last_be[e],
                    self.value.eq(~(regs[e][::-1])),
                    self.error.eq(engines[e].next != self.check))
                        for e in range(dw)]

# MAC CRC Inserter ---------------------------------------------------------------------------------

class LiteEthMACCRCInserter(Module):
    """CRC Inserter

    Append a CRC at the end of each packet.

    Parameters
    ----------
    description : description
        description of the dataflow.

    Attributes
    ----------
    sink : in
        Packets octets without CRC.
    source : out
        Packets octets with CRC.
    """
    def __init__(self, crc_class, description):
        self.sink   = sink = stream.Endpoint(description)
        self.source = source = stream.Endpoint(description)

        # # #

        dw  = len(sink.data)
        crc = crc_class(dw)
        fsm = FSM(reset_state="IDLE")
        self.submodules += crc, fsm

        fsm.act("IDLE",
            crc.reset.eq(1),
            sink.ready.eq(1),
            If(sink.valid,
                sink.ready.eq(0),
                NextState("COPY"),
            )
        )
        fsm.act("COPY",
            crc.ce.eq(sink.valid & source.ready),
            crc.data.eq(sink.data),
            sink.connect(source),
            source.last.eq(0),
            If(sink.valid & sink.last & source.ready,
                NextState("CRC"),
            )
        )
        ratio = crc.width//dw
        if ratio > 1:
            cnt = Signal(max=ratio, reset=ratio-1)
            cnt_done = Signal()
            fsm.act("CRC",
                source.valid.eq(1),
                chooser(crc.value, cnt, source.data, reverse=True),
                If(cnt_done,
                    source.last.eq(1),
                    If(source.ready, NextState("IDLE"))
                )
            )
            self.comb += cnt_done.eq(cnt == 0)
            self.sync += \
                If(fsm.ongoing("IDLE"),
                    cnt.eq(cnt.reset)
                ).Elif(fsm.ongoing("CRC") & ~cnt_done,
                    cnt.eq(cnt - source.ready)
                )
        else:
            fsm.act("CRC",
                source.valid.eq(1),
                source.last.eq(1),
                source.data.eq(crc.value),
                If(source.ready, NextState("IDLE"))
            )


class LiteEthMACCRC32Inserter(LiteEthMACCRCInserter):
    def __init__(self, description):
        LiteEthMACCRCInserter.__init__(self, LiteEthMACCRC32, description)

# MAC CRC Checker ----------------------------------------------------------------------------------

class LiteEthMACCRCChecker(Module):
    """CRC Checker

    Check CRC at the end of each packet.

    Parameters
    ----------
    description : description
        description of the dataflow.

    Attributes
    ----------
    sink : in
        Packet octets with CRC.
    source : out
        Packet octets without CRC and "error" set to 0
        on last when CRC OK / set to 1 when CRC KO.
    error : out
        Pulses every time a CRC error is detected.
    """
    def __init__(self, crc_class, description):
        self.sink   = sink   = stream.Endpoint(description)
        self.source = source = stream.Endpoint(description)

        self.error = Signal()

        # # #

        dw  = len(sink.data)
        crc = crc_class(dw)
        self.submodules += crc
        ratio = crc.width//dw

        fifo = ResetInserter()(stream.SyncFIFO(description, ratio + 1))
        self.submodules += fifo

        fsm = FSM(reset_state="RESET")
        self.submodules += fsm

        fifo_in   = Signal()
        fifo_out  = Signal()
        fifo_full = Signal()

        self.comb += [
            fifo_full.eq(fifo.level == ratio),
            fifo_in.eq(sink.valid & (~fifo_full | fifo_out)),
            fifo_out.eq(source.valid & source.ready),

            sink.connect(fifo.sink),
            fifo.sink.valid.eq(fifo_in),
            self.sink.ready.eq(fifo_in),

            source.valid.eq(sink.valid & fifo_full),
            source.last.eq(sink.last),
            fifo.source.ready.eq(fifo_out),
            source.payload.eq(fifo.source.payload),

            source.error.eq(sink.error | crc.error),
            self.error.eq(source.valid & source.last & crc.error),
        ]

        fsm.act("RESET",
            crc.reset.eq(1),
            fifo.reset.eq(1),
            NextState("IDLE"),
        )
        self.comb += crc.data.eq(sink.data)
        fsm.act("IDLE",
            If(sink.valid & sink.ready,
                crc.ce.eq(1),
                NextState("COPY")
            )
        )
        fsm.act("COPY",
            If(sink.valid & sink.ready,
                crc.ce.eq(1),
                If(sink.last,
                    NextState("RESET")
                )
            )
        )


class LiteEthMACCRC32Checker(LiteEthMACCRCChecker):
    def __init__(self, description):
        LiteEthMACCRCChecker.__init__(self, LiteEthMACCRC32, description)
