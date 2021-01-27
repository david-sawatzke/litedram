from operator import or_
from functools import reduce
from collections import defaultdict

from migen import *

from litex.soc.interconnect.csr import *

from litedram.common import *
from litedram.phy.dfi import *

from litedram.phy.lpddr4.utils import bitpattern, delayed, ConstBitSlip, DQSPattern, Serializer, Deserializer
from litedram.phy.lpddr4.commands import DFIPhaseAdapter


class Latency:
    def __init__(self, sys, sys8x=0):
        self.sys = sys + sys8x//8
        self.sys8x = sys8x % 8

    def __add__(self, other):
        return Latency(sys=self.sys + other.sys, sys8x=self.sys8x + other.sys8x)

    def __repr__(self):
        return "Latency(sys={}, sys8x={})".format(self.sys, self.sys8x)


class LPDDR4Output:
    """Unserialized output of LPDDR4PHY. Has to be serialized by concrete implementation."""
    def __init__(self, nphases, databits):
        # Pads: RESET_N, CS, CKE, CK, CA[5:0], DMI[1:0], DQ[15:0], DQS[1:0], ODT_CA
        self.clk     = Signal(2*nphases)
        self.cke     = Signal(nphases)
        self.odt     = Signal(nphases)
        self.reset_n = Signal(nphases)
        self.cs      = Signal(nphases)
        self.ca      = [Signal(nphases)   for _ in range(6)]
        self.dmi_o   = [Signal(2*nphases) for _ in range(2)]
        self.dmi_i   = [Signal(2*nphases) for _ in range(2)]
        self.dmi_oe  = Signal()  # no serialization
        self.dq_o    = [Signal(2*nphases) for _ in range(databits)]
        self.dq_i    = [Signal(2*nphases) for _ in range(databits)]
        self.dq_oe   = Signal()  # no serialization
        self.dqs_o   = [Signal(2*nphases) for _ in range(2)]
        self.dqs_i   = [Signal(2*nphases) for _ in range(2)]
        self.dqs_oe  = Signal()  # no serialization


class LPDDR4PHY(Module, AutoCSR):
    def __init__(self, pads, *,
                 sys_clk_freq, ser_latency, des_latency, phytype,
                 masked_write=True, cmd_delay=None):
        self.pads        = pads
        self.memtype     = memtype     = "LPDDR4"
        self.nranks      = nranks      = 1 if not hasattr(pads, "cs_n") else len(pads.cs_n)
        self.databits    = databits    = len(pads.dq)
        self.addressbits = addressbits = 17  # for activate row address
        self.bankbits    = bankbits    = 3
        self.nphases     = nphases     = 8
        self.tck         = tck         = 1 / (nphases*sys_clk_freq)
        assert databits % 8 == 0

        # Parameters -------------------------------------------------------------------------------
        def get_cl_cw(memtype, tck):
            # MT53E256M16D1, No DBI, Set A
            f_to_cl_cwl = OrderedDict()
            f_to_cl_cwl[ 532e6] = ( 6,  4)
            f_to_cl_cwl[1066e6] = (10,  6)
            f_to_cl_cwl[1600e6] = (14,  8)
            f_to_cl_cwl[2132e6] = (20, 10)
            f_to_cl_cwl[2666e6] = (24, 12)
            f_to_cl_cwl[3200e6] = (28, 14)
            f_to_cl_cwl[3732e6] = (32, 16)
            f_to_cl_cwl[4266e6] = (36, 18)
            for f, (cl, cwl) in f_to_cl_cwl.items():
                if tck >= 2/f:
                    return cl, cwl
            raise ValueError

        # Bitslip introduces latency between from `cycles` up to `cycles + 1`
        bitslip_cycles  = 1
        # Commands are sent over 4 DRAM clocks (sys8x) and we count cl/cwl from last bit
        cmd_latency     = 4  # FIXME: or should it be 3?
        # Commands read from adapters are delayed on ConstBitSlips
        ca_latency      = 1

        cl, cwl         = get_cl_cw(memtype, tck)
        cl_sys_latency  = get_sys_latency(nphases, cl)
        cwl_sys_latency = get_sys_latency(nphases, cwl)
        # For reads we need to account for ser+des latency to make sure we get the data in-phase with sys clock
        rdphase = get_sys_phase(nphases, cl_sys_latency, cl + cmd_latency + ser_latency.sys8x + des_latency.sys8x)
        # No need to modify wrphase, because ser_latency applies the same to both CA and DQ
        wrphase = get_sys_phase(nphases, cwl_sys_latency, cwl + cmd_latency)

        # When the calculated phase is negative, it means that we need to increase sys latency
        def updated_latency(phase, sys_latency):
            while phase < 0:
                phase += nphases
                sys_latency += 1
            return phase, sys_latency

        wrphase, cwl_sys_latency = updated_latency(wrphase, cwl_sys_latency)
        rdphase, cl_sys_latency = updated_latency(rdphase, cl_sys_latency)

        # Read latency
        read_data_delay = ca_latency + ser_latency.sys + cl_sys_latency  # DFI cmd -> read data on DQ
        read_des_delay  = des_latency.sys + bitslip_cycles  # data on DQ -> data on DFI rddata
        read_latency    = read_data_delay + read_des_delay

        # Write latency
        write_latency = cwl_sys_latency

        # Registers --------------------------------------------------------------------------------
        self._rst             = CSRStorage()

        self._wlevel_en     = CSRStorage()
        self._wlevel_strobe = CSR()

        self._dly_sel = CSRStorage(databits//8)

        self._rdly_dq_bitslip_rst = CSR()
        self._rdly_dq_bitslip     = CSR()

        self._wdly_dq_bitslip_rst = CSR()
        self._wdly_dq_bitslip     = CSR()

        self._rdphase = CSRStorage(log2_int(nphases), reset=rdphase)
        self._wrphase = CSRStorage(log2_int(nphases), reset=wrphase)

        # PHY settings -----------------------------------------------------------------------------
        self.settings = PhySettings(
            phytype       = phytype,
            memtype       = memtype,
            databits      = databits,
            dfi_databits  = 2*databits,
            nranks        = nranks,
            nphases       = nphases,
            rdphase       = self._rdphase.storage,
            wrphase       = self._wrphase.storage,
            cl            = cl,
            cwl           = cwl,
            read_latency  = read_latency,
            write_latency = write_latency,
            cmd_latency   = cmd_latency,
            cmd_delay     = cmd_delay,
        )

        # DFI Interface ----------------------------------------------------------------------------
        # Due to the fact that LPDDR4 has 16n prefetch we use 8 phases to be able to read/write a
        # whole burst during a single controller clock cycle. PHY should use sys8x clock.
        self.dfi = dfi = Interface(addressbits, bankbits, nranks, 2*databits, nphases=8)

        # # #

        adapters = [DFIPhaseAdapter(phase, masked_write=masked_write) for phase in self.dfi.phases]
        self.submodules += adapters

        # Now prepare the data by converting the sequences on adapters into sequences on the pads.
        # We have to ignore overlapping commands, and module timings have to ensure that there are
        # no overlapping commands anyway.
        self.out = LPDDR4Output(nphases, databits)

        # Clocks -----------------------------------------------------------------------------------
        self.comb += self.out.clk.eq(bitpattern("-_-_-_-_" * 2))

        # Simple commands --------------------------------------------------------------------------
        self.comb += [
            self.out.cke.eq(Cat(delayed(self, phase.cke) for phase in self.dfi.phases)),
            self.out.odt.eq(Cat(delayed(self, phase.odt) for phase in self.dfi.phases)),
            self.out.reset_n.eq(Cat(delayed(self, phase.reset_n) for phase in self.dfi.phases)),
        ]

        # LPDDR4 Commands --------------------------------------------------------------------------
        # Each command can span several phases (up to 4), so we must ignore overlapping commands,
        # but in general, module timings should be set in a way that overlapping will never happen.

        # Create a history of valid adapters used for masking overlapping ones.
        # TODO: make optional, as it takes up resources and the controller should ensure no overlaps
        valids = ConstBitSlip(dw=nphases, cycles=1, slp=0)
        self.submodules += valids
        self.comb += valids.i.eq(Cat(a.valid for a in adapters))
        # valids_hist = valids.r
        valids_hist = Signal.like(valids.r)
        # TODO: especially make this part optional
        for i in range(len(valids_hist)):
            was_valid_before = reduce(or_, valids_hist[max(0, i-3):i], 0)
            self.comb += valids_hist[i].eq(valids.r[i] & ~was_valid_before)

        cs_per_adapter = []
        ca_per_adapter = defaultdict(list)
        for phase, adapter in enumerate(adapters):
            # The signals from an adapter can be used if there were no commands on 3 previous cycles
            allowed = ~reduce(or_, valids_hist[nphases+phase - 3:nphases+phase])

            # Use CS and CA of given adapter slipped by `phase` bits
            cs_bs = ConstBitSlip(dw=nphases, cycles=1, slp=phase)
            self.submodules += cs_bs
            self.comb += cs_bs.i.eq(Cat(adapter.cs)),
            cs_mask = Replicate(allowed, len(cs_bs.o))
            cs = cs_bs.o & cs_mask
            cs_per_adapter.append(cs)

            # For CA we need to do the same for each bit
            ca_bits = []
            for bit in range(6):
                ca_bs = ConstBitSlip(dw=nphases, cycles=1, slp=phase)
                self.submodules += ca_bs
                ca_bit_hist = [adapter.ca[i][bit] for i in range(4)]
                self.comb += ca_bs.i.eq(Cat(*ca_bit_hist)),
                ca_mask = Replicate(allowed, len(ca_bs.o))
                ca = ca_bs.o & ca_mask
                ca_per_adapter[bit].append(ca)

        # OR all the masked signals
        self.comb += self.out.cs.eq(reduce(or_, cs_per_adapter))
        for bit in range(6):
            self.comb += self.out.ca[bit].eq(reduce(or_, ca_per_adapter[bit]))

        # DQ ---------------------------------------------------------------------------------------
        dq_oe = Signal()
        self.comb += self.out.dq_oe.eq(delayed(self, dq_oe, cycles=1))

        for bit in range(self.databits):
            # output
            wrdata = [
                self.dfi.phases[i//2].wrdata[i%2 * self.databits + bit]
                for i in range(2*nphases)
            ]
            self.submodules += BitSlip(
                dw     = 2*nphases,
                cycles = bitslip_cycles,
                rst    = self.get_rst(bit//8, self._wdly_dq_bitslip_rst),
                slp    = self.get_slp(bit//8, self._wdly_dq_bitslip),
                i      = Cat(*wrdata),
                o      = self.out.dq_o[bit],
            )

            # input
            dq_i_bs = Signal(2*nphases)
            self.submodules += BitSlip(
                dw     = 2*nphases,
                cycles = bitslip_cycles,
                rst    = self.get_rst(bit//8, self._rdly_dq_bitslip_rst),
                slp    = self.get_slp(bit//8, self._rdly_dq_bitslip),
                i      = self.out.dq_i[bit],
                o      = dq_i_bs,
            )
            for i in range(2*nphases):
                self.comb += self.dfi.phases[i//2].rddata[i%2 * self.databits + bit].eq(dq_i_bs[i])

        # DQS --------------------------------------------------------------------------------------
        dqs_oe        = Signal()
        dqs_preamble  = Signal()
        dqs_postamble = Signal()
        dqs_pattern   = DQSPattern(
            preamble      = dqs_preamble,  # FIXME: are defined the opposite way (common.py) ???
            postamble     = dqs_postamble,
            wlevel_en     = self._wlevel_en.storage,
            wlevel_strobe = self._wlevel_strobe.re)
        self.submodules += dqs_pattern
        self.comb += [
            self.out.dqs_oe.eq(delayed(self, dqs_oe, cycles=1)),
        ]

        for byte in range(self.databits//8):
            # output
            self.submodules += BitSlip(
                dw     = 2*nphases,
                cycles = bitslip_cycles,
                rst    = self.get_rst(byte, self._wdly_dq_bitslip_rst),
                slp    = self.get_slp(byte, self._wdly_dq_bitslip),
                i      = dqs_pattern.o,
                o      = self.out.dqs_o[byte],
            )

        # DMI --------------------------------------------------------------------------------------
        # DMI signal is used for Data Mask or Data Bus Invertion depending on Mode Registers values.
        # With DM and DBI disabled, this signal is a Don't Care.
        # With DM enabled, masking is performed only when the command used is WRITE-MASKED.
        # We don't support DBI, DM support is configured statically with `masked_write`.
        for byte in range(self.databits//8):
            if not masked_write:
                self.comb += self.out.dmi_o[byte].eq(0)
                self.comb += self.out.dmi_oe.eq(0)
            else:
                self.comb += self.out.dmi_oe.eq(self.out.dq_oe)
                wrdata_mask = [
                    self.dfi.phases[i//2] .wrdata_mask[i%2 * self.databits//8 + byte]
                    for i in range(2*nphases)
                ]
                self.submodules += BitSlip(
                    dw     = 2*nphases,
                    cycles = bitslip_cycles,
                    rst    = self.get_rst(byte, self._wdly_dq_bitslip_rst),
                    slp    = self.get_slp(byte, self._wdly_dq_bitslip),
                    i      = Cat(*wrdata_mask),
                    o      = self.out.dmi_o[byte],
                )

        # Read Control Path ------------------------------------------------------------------------
        # Creates a delay line of read commands coming from the DFI interface. The output is used to
        # signal a valid read data to the DFI interface.
        #
        # The read data valid is asserted for 1 sys_clk cycle when the data is available on the DFI
        # interface, the latency is the sum of the OSERDESE2, CAS, ISERDESE2 and Bitslip latencies.
        rddata_en = TappedDelayLine(
            signal = reduce(or_, [dfi.phases[i].rddata_en for i in range(nphases)]),
            ntaps  = self.settings.read_latency
        )
        self.submodules += rddata_en

        self.comb += [
            phase.rddata_valid.eq(rddata_en.output | self._wlevel_en.storage)
            for phase in dfi.phases
        ]

        # Write Control Path -----------------------------------------------------------------------
        wrtap = cwl_sys_latency - 1
        assert wrtap >= 0

        # Create a delay line of write commands coming from the DFI interface. This taps are used to
        # control DQ/DQS tristates.
        wrdata_en = TappedDelayLine(
            signal = reduce(or_, [dfi.phases[i].wrdata_en for i in range(nphases)]),
            ntaps  = wrtap + 2
        )
        self.submodules += wrdata_en

        self.comb += dq_oe.eq(wrdata_en.taps[wrtap])
        # Always enabled in write leveling mode, else during transfers
        self.comb += dqs_oe.eq(self._wlevel_en.storage | (dqs_preamble | dq_oe | dqs_postamble))

        # Write DQS Postamble/Preamble Control Path ------------------------------------------------
        # Generates DQS Preamble 1 cycle before the first write and Postamble 1 cycle after the last
        # write. During writes, DQS tristate is configured as output for at least 3 sys_clk cycles:
        # 1 for Preamble, 1 for the Write and 1 for the Postamble.
        def wrdata_en_tap(i):  # allows to have wrtap == 0
            return wrdata_en.input if i == -1 else wrdata_en.taps[i]
        self.comb += dqs_preamble.eq( wrdata_en_tap(wrtap - 1)  & ~wrdata_en_tap(wrtap + 0))
        self.comb += dqs_postamble.eq(wrdata_en_tap(wrtap + 1)  & ~wrdata_en_tap(wrtap + 0))

    def get_rst(self, byte, rst_csr):
        return (self._dly_sel.storage[byte] & rst_csr.re) | self._rst.storage

    def get_slp(self, byte, slp_csr):
        return self._dly_sel.storage[byte] & slp_csr.re


class DoubleRateLPDDR4PHY(LPDDR4PHY):
    """LPDDR4PHY wrapper that performs one stage of serialization (16:8)

    Needed for targets that only have hardware serialization blocks up to 8:1.
    """
    def __init__(self, pads, *, ser_latency, des_latency, serdes_reset_value=0, **kwargs):
        super().__init__(pads,
            ser_latency = ser_latency + Latency(sys=Serializer.LATENCY),
            des_latency = des_latency + Latency(sys=Deserializer.LATENCY),
            **kwargs)

        self._out = self.out
        self.out = LPDDR4Output(nphases=self.nphases//2, databits=self.databits)

        def ser(i, o):
            assert len(o) == len(i)//2
            self.submodules += Serializer(
                clkdiv      = "sys",
                clk         = "sys2x",
                i_dw        = len(i),
                o_dw        = len(o),
                i           = i,
                o           = o,
                reset_value = serdes_reset_value,
            )

        def des(i, o):
            assert len(i) == len(o)//2
            self.submodules += Deserializer(
                clkdiv      = "sys",
                clk         = "sys2x",
                i_dw        = len(i),
                o_dw        = len(o),
                i           = i,
                o           = o,
                reset_value = serdes_reset_value,
            )

        # handle ser/des for both the lists (like dq) and just Signal (like cs)
        def apply(fn, i, o):
            if not isinstance(i, list):
                i, o = [i], [o]
            for i_n, o_n in zip(i, o):
                fn(i=i_n, o=o_n)

        for name in vars(self.out):
            old = getattr(self._out, name)
            new = getattr(self.out, name)
            if name.endswith("_oe"):  # OE signals need to be delayed
                self.comb += new.eq(delayed(self, old, cycles=Serializer.LATENCY))
            elif name.endswith("_i"):  # Deserialize inputs
                apply(des, o=old, i=new)
            else:  # All other signals are outputs
                apply(ser, i=old, o=new)
