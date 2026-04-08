"""
Comprehensive knowledge base of Intel TMA (Top-Down Microarchitecture Analysis) metrics.

Covers the full superset across Skylake (SKL), Ice Lake (ICL), Sapphire Rapids (SPR),
Granite Rapids (GNR), and Panther Lake (PTL) microarchitectures (120+ nodes).

Each metric entry contains:
    - description: what the metric measures
    - typical_causes: common causes when the metric is elevated
    - tuning_hints: actionable tuning suggestions for Redis/database workloads
    - level: TMA hierarchy level (1-6)
    - parent: parent node name (dot-separated path) or None for L1

Helper functions:
    - get_metric_info(name): lookup by exact or partial name
    - list_all_metrics(): sorted list of all metric names
    - get_children(name): direct children of a node
    - get_parent(name): parent node name
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# The knowledge base
# ---------------------------------------------------------------------------

METRICS_KB: Dict[str, Dict[str, Any]] = {

    # ===================================================================
    # LEVEL 1
    # ===================================================================

    "Frontend_Bound": {
        "description": (
            "Fraction of pipeline slots where the frontend (instruction fetch and "
            "decode) was unable to deliver a micro-op to the backend.  A high value "
            "means the CPU is starved for instructions."
        ),
        "typical_causes": [
            "Large instruction footprint causing I-cache misses",
            "Inefficient branch prediction causing pipeline flushes",
            "Hot code spanning many pages leading to ITLB pressure",
            "Suboptimal code layout preventing DSB (uop cache) utilization",
        ],
        "tuning_hints": [
            "Profile with `perf record -e frontend_retired.latency_ge_8` to find hot fetch-stalled code",
            "Use PGO (Profile-Guided Optimization) or BOLT to improve code layout",
            "Reduce binary bloat: prefer -Os over -O3 for rarely-executed helpers",
            "For Redis modules, keep hot command handlers small and contiguous",
            "Consider hugepages for code (transparent hugepages or explicit) to reduce ITLB misses",
        ],
        "level": 1,
        "parent": None,
    },

    "Bad_Speculation": {
        "description": (
            "Fraction of pipeline slots wasted due to incorrect speculation, "
            "including branch mispredictions and machine clears.  Work was performed "
            "but ultimately discarded."
        ),
        "typical_causes": [
            "Unpredictable branches (e.g., hash-table lookups, polymorphic dispatch)",
            "Indirect calls through function pointers with many targets",
            "Self-modifying code or memory ordering violations",
            "Frequent context switches polluting branch predictor state",
        ],
        "tuning_hints": [
            "Replace unpredictable branches with branchless code (cmov, bitwise ops)",
            "Sort data to make branches more predictable where possible",
            "Use __builtin_expect / likely()/unlikely() for skewed branches",
            "In Redis, reduce polymorphic dispatch in hot paths (e.g., object encoding checks)",
            "Minimize indirect calls; use switch-case or direct calls in hot loops",
        ],
        "level": 1,
        "parent": None,
    },

    "Backend_Bound": {
        "description": (
            "Fraction of pipeline slots where the backend could not accept "
            "micro-ops because execution resources or memory subsystem were busy.  "
            "This is the dominant bottleneck for most data-intensive workloads."
        ),
        "typical_causes": [
            "Cache misses at L1/L2/L3 or DRAM latency",
            "Execution port contention or long-latency operations (divides, etc.)",
            "Store-buffer full stalls",
            "Memory bandwidth saturation on multi-socket systems",
        ],
        "tuning_hints": [
            "Distinguish Memory_Bound vs Core_Bound at Level 2 before tuning",
            "For Redis: optimize data structure layout for cache-friendliness (struct packing, SDS alignment)",
            "Use hardware prefetch or explicit __builtin_prefetch for pointer-chasing workloads",
            "Pin Redis processes to a single NUMA node to avoid cross-socket traffic",
            "Consider io_uring or large-page backing for mmap regions",
        ],
        "level": 1,
        "parent": None,
    },

    "Retiring": {
        "description": (
            "Fraction of pipeline slots utilized by micro-ops that eventually retire "
            "(i.e., useful work).  A high Retiring value is generally good, but very "
            "high values may indicate microcode assists or inefficient instruction mix."
        ),
        "typical_causes": [
            "Efficient code with high IPC (good)",
            "Heavy use of microcode-sequenced instructions inflating uop count",
            "Scalar code that could benefit from vectorization",
            "Excessive NOP padding or alignment directives",
        ],
        "tuning_hints": [
            "If Retiring is high but throughput is low, check Heavy_Operations",
            "Look at Light_Operations vs Heavy_Operations split to gauge instruction efficiency",
            "For Redis string operations, consider SIMD-accelerated memcpy/memcmp",
            "Verify compiler is not generating unnecessary REP-prefixed instructions",
            "High Retiring with low CPI is the ideal state -- focus tuning elsewhere",
        ],
        "level": 1,
        "parent": None,
    },

    # ===================================================================
    # LEVEL 2
    # ===================================================================

    # --- Frontend_Bound children ---

    "Frontend_Bound.Fetch_Latency": {
        "description": (
            "Frontend stalls caused by latency in fetching instructions.  The "
            "instruction fetch pipeline is waiting for cache/TLB lookups or branch "
            "resolution before it can deliver micro-ops."
        ),
        "typical_causes": [
            "I-cache misses on large binaries",
            "ITLB misses when code spans many 4 KB pages",
            "Branch resteers after mispredictions causing fetch bubbles",
            "Microcode sequencer switches for complex instructions",
        ],
        "tuning_hints": [
            "Drill into L3 children: ICache_Misses, ITLB_Misses, Branch_Resteers",
            "Use BOLT or AutoFDO to colocate hot code and reduce I-cache pressure",
            "Map hot Redis modules into hugepages to cut ITLB misses",
            "Reduce binary size of hot paths; split cold error-handling code out of line",
        ],
        "level": 2,
        "parent": "Frontend_Bound",
    },

    "Frontend_Bound.Fetch_Bandwidth": {
        "description": (
            "Frontend stalls where the fetch unit is operational but cannot deliver "
            "enough micro-ops per cycle to saturate the backend.  The decoders or uop "
            "cache are delivering fewer than the maximum width."
        ),
        "typical_causes": [
            "Code not fitting in the DSB (uop cache) forcing MITE (legacy decode) path",
            "Length-changing prefixes (LCP) causing decoder slowdowns",
            "Suboptimal instruction alignment reducing decode throughput",
            "Too many branches within a 32-byte window preventing DSB usage",
        ],
        "tuning_hints": [
            "Check DSB vs MITE ratio to see if uop cache coverage is adequate",
            "Align hot loop entries to 32-byte boundaries for DSB efficiency",
            "Avoid length-changing prefixes (common with address-size overrides)",
            "Use -march=native to let the compiler use optimal instruction encodings",
            "Consider link-time optimization (LTO) to eliminate cold code from hot sections",
        ],
        "level": 2,
        "parent": "Frontend_Bound",
    },

    # --- Bad_Speculation children ---

    "Bad_Speculation.Branch_Mispredicts": {
        "description": (
            "Slots wasted because the branch predictor chose the wrong path.  "
            "The pipeline must be flushed and restarted from the correct target."
        ),
        "typical_causes": [
            "Data-dependent branches in hash-table probing or tree traversal",
            "Indirect jumps through function pointer tables with many targets",
            "Switch statements with many cases compiled to indirect jumps",
            "Branches with ~50% taken rate (maximum unpredictability)",
        ],
        "tuning_hints": [
            "Profile with `perf record -e br_misp_retired.all_branches` to find mispredicting branches",
            "Convert data-dependent branches to cmov or arithmetic (branchless min/max)",
            "In Redis, consider branchless encoding checks for ziplist/listpack access",
            "Partition switch statements: keep frequent cases first or use computed goto",
            "Batch similar operations to improve branch predictor pattern learning",
        ],
        "level": 2,
        "parent": "Bad_Speculation",
    },

    "Bad_Speculation.Machine_Clears": {
        "description": (
            "Slots wasted due to machine clears -- full pipeline flushes caused by "
            "events other than branch mispredictions, such as memory ordering "
            "violations, self-modifying code, or floating-point assists."
        ),
        "typical_causes": [
            "Memory ordering violations (store-to-load forwarding conflicts)",
            "Self-modifying code or cross-modifying code (JIT compilers)",
            "Floating-point denormal or SNaN operands triggering assists",
            "Machine clear due to memory disambiguation failure",
        ],
        "tuning_hints": [
            "Check for store-to-load forwarding failures with `perf stat -e machine_clears.memory_ordering`",
            "Avoid overlapping stores and loads to the same address with different sizes",
            "Flush denormals to zero with _MM_SET_FLUSH_ZERO_MODE if precision allows",
            "For Redis Lua JIT (LuaJIT), ensure code cache invalidation is minimized",
            "Avoid false sharing between producer/consumer threads on adjacent cache lines",
        ],
        "level": 2,
        "parent": "Bad_Speculation",
    },

    # --- Backend_Bound children ---

    "Backend_Bound.Memory_Bound": {
        "description": (
            "Fraction of backend stalls caused by the memory subsystem -- loads "
            "waiting for data from caches or main memory, or stores waiting for "
            "buffer space."
        ),
        "typical_causes": [
            "Working set exceeding L1/L2/L3 cache sizes",
            "Pointer-chasing data structures (linked lists, trees, hash chains)",
            "Irregular memory access patterns defeating hardware prefetcher",
            "NUMA remote memory accesses in multi-socket configurations",
        ],
        "tuning_hints": [
            "Drill down to L1_Bound..DRAM_Bound to find the cache level bottleneck",
            "For Redis, tune maxmemory and eviction to keep hot data in LLC",
            "Prefer compact data structures (listpack, intset) over pointer-heavy ones (dict, skiplist) when possible",
            "Use `numactl --membind` to keep Redis data on the local NUMA node",
            "Consider hardware prefetch hints for sequential scans (SCAN command hot paths)",
        ],
        "level": 2,
        "parent": "Backend_Bound",
    },

    "Backend_Bound.Core_Bound": {
        "description": (
            "Fraction of backend stalls caused by execution unit limitations -- "
            "insufficient port bandwidth, long-latency operations (divides, "
            "serializing instructions), or dependency chains."
        ),
        "typical_causes": [
            "Long dependency chains limiting instruction-level parallelism",
            "Frequent integer or floating-point divides",
            "Serializing instructions (CPUID, WRMSR, locked operations)",
            "Port contention from unbalanced instruction mix",
        ],
        "tuning_hints": [
            "Drill into Divider, Serializing_Operation, Ports_Utilization",
            "Break long dependency chains by accumulating into multiple variables",
            "Replace divides with shifts or multiplicative inverses where possible",
            "Reduce locked atomic operations; use thread-local accumulators then merge",
            "For Redis, minimize LOCK-prefixed increments on shared statistics counters",
        ],
        "level": 2,
        "parent": "Backend_Bound",
    },

    # --- Retiring children ---

    "Retiring.Light_Operations": {
        "description": (
            "Slots used by simple micro-ops that map 1:1 to architectural "
            "instructions (single-uop instructions).  This is the most efficient "
            "form of retiring."
        ),
        "typical_causes": [
            "Simple ALU, load, store, and branch instructions (good)",
            "Well-optimized inner loops with high IPC",
        ],
        "tuning_hints": [
            "High Light_Operations is generally ideal -- focus tuning elsewhere",
            "If throughput is still low, check FP_Arith and Memory_Operations sub-nodes",
            "Verify vectorization is being applied where beneficial (FP_Vector, Int_Vector)",
            "For Redis, high Light_Operations during GET/SET is expected and healthy",
        ],
        "level": 2,
        "parent": "Retiring",
    },

    "Retiring.Heavy_Operations": {
        "description": (
            "Slots used by instructions that require more than one micro-op "
            "(microcode-sequenced or multi-uop instructions).  These consume more "
            "pipeline resources per instruction."
        ),
        "typical_causes": [
            "REP-prefixed string operations (REP MOVSB, REP STOSB)",
            "Complex instructions triggering microcode sequencer (CPUID, XSAVE)",
            "Assists for denormal floating-point or page faults",
            "CISC instructions with memory operands requiring load+compute+store uops",
        ],
        "tuning_hints": [
            "Check Microcode_Sequencer sub-node for assist-driven overhead",
            "Replace REP MOVSB with optimized memcpy for small known sizes",
            "Avoid CISC memory-operand forms in hot loops; prefer load-compute-store",
            "For Redis, audit string operation helpers for unnecessary REP usage",
            "High Heavy_Operations with low throughput is a red flag -- drill deeper",
        ],
        "level": 2,
        "parent": "Retiring",
    },

    # ===================================================================
    # LEVEL 3
    # ===================================================================

    # --- Fetch_Latency children ---

    "Frontend_Bound.Fetch_Latency.ICache_Misses": {
        "description": (
            "Frontend stalls caused by instruction cache (L1I) misses.  "
            "The processor must fetch instructions from L2 or beyond."
        ),
        "typical_causes": [
            "Large binary with scattered hot functions",
            "Frequent calls between distant code sections",
            "Dynamic linking pulling in many shared libraries",
            "JIT-compiled code (LuaJIT in Redis) with poor locality",
        ],
        "tuning_hints": [
            "Use BOLT/AutoFDO to reorder functions by execution frequency",
            "Link hot functions together with linker scripts or __attribute__((section))",
            "Reduce number of shared libraries loaded at runtime",
            "For Redis modules, keep hot-path code in a single compilation unit",
            "Consider -ffunction-sections -fdata-sections with --gc-sections to strip dead code",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Latency",
    },

    "Frontend_Bound.Fetch_Latency.ITLB_Misses": {
        "description": (
            "Frontend stalls caused by instruction TLB misses.  The processor "
            "must perform a page walk to translate the instruction virtual address."
        ),
        "typical_causes": [
            "Code spanning hundreds of 4 KB pages",
            "Large binaries or many shared libraries",
            "Hot code scattered across non-contiguous pages",
            "JIT code allocated in separate mmap regions",
        ],
        "tuning_hints": [
            "Map executable pages with 2 MB hugepages (transparent or explicit)",
            "Use BOLT to compact hot code into fewer pages",
            "Reduce total code footprint with LTO and dead-code elimination",
            "For Redis, compile with -ffunction-sections and link hot functions contiguously",
            "Check /proc/<pid>/smaps for code page counts and THP eligibility",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Latency",
    },

    "Frontend_Bound.Fetch_Latency.Branch_Resteers": {
        "description": (
            "Frontend stalls caused by branch resteers -- the fetch unit must "
            "redirect to a new address after a branch misprediction, BTB miss, "
            "or unknown branch."
        ),
        "typical_causes": [
            "Branch mispredictions causing fetch pipeline flush",
            "First-time execution of branches not yet in BTB",
            "Indirect branches with many targets",
            "Machine clears causing full pipeline resteer",
        ],
        "tuning_hints": [
            "Reduce branch mispredictions (see Branch_Mispredicts tuning hints)",
            "Use PGO to train branch predictor tables and improve BTB coverage",
            "Linearize hot code paths to reduce taken branches",
            "For Redis command dispatch, consider grouping common commands together",
            "Drill into Mispredicts_Resteers vs Unknown_Branches to distinguish causes",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Latency",
    },

    "Frontend_Bound.Fetch_Latency.MS_Switches": {
        "description": (
            "Frontend stalls caused by switching from the regular decode pipeline "
            "to the microcode sequencer (MS).  Each switch costs several cycles."
        ),
        "typical_causes": [
            "Complex instructions requiring microcode (CPUID, XSAVE, REP-prefixed)",
            "Assists triggered by denormals, page faults, or AC checks",
            "Frequent transitions between simple and complex instruction streams",
        ],
        "tuning_hints": [
            "Avoid complex instructions in hot loops (REP MOVSB for small copies)",
            "Pre-fault pages to avoid assist-driven MS switches during hot paths",
            "Flush denormals to zero if precision is not critical",
            "For Redis, avoid CPUID or XGETBV in performance-critical paths",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Latency",
    },

    "Frontend_Bound.Fetch_Latency.LCP": {
        "description": (
            "Frontend stalls caused by length-changing prefixes (LCP).  Certain "
            "instruction prefixes force the pre-decoder to re-examine instruction "
            "length, adding latency."
        ),
        "typical_causes": [
            "Operand-size override prefix (0x66) on instructions with immediate operands",
            "Address-size override prefix (0x67) in 64-bit mode",
            "Compiler generating 16-bit operand forms unnecessarily",
        ],
        "tuning_hints": [
            "Compile with -march=native to avoid unnecessary prefix generation",
            "Avoid explicit 16-bit register usage in inline assembly",
            "This is rarely a significant bottleneck; fix only if clearly elevated",
            "Check compiler output for unexpected operand-size prefixes in hot loops",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Latency",
    },

    "Frontend_Bound.Fetch_Latency.DSB_Switches": {
        "description": (
            "Frontend stalls caused by switching between the DSB (decoded uop cache) "
            "and the MITE (legacy decode) paths.  Each switch incurs a pipeline bubble."
        ),
        "typical_causes": [
            "Hot code partially fitting in DSB, causing alternation",
            "Branches crossing 32-byte DSB line boundaries",
            "Self-modifying code invalidating DSB entries",
        ],
        "tuning_hints": [
            "Align hot loops to 32-byte boundaries to maximize DSB coverage",
            "Use BOLT to optimize code layout for DSB residency",
            "Avoid mixing very short and very long instruction sequences",
            "This metric is secondary; prioritize ICache_Misses and ITLB_Misses first",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Latency",
    },

    # --- Fetch_Bandwidth children ---

    "Frontend_Bound.Fetch_Bandwidth.MITE": {
        "description": (
            "Fraction of uops delivered by the legacy MITE (Macro Instruction "
            "Translation Engine) decode path rather than the DSB.  MITE delivers "
            "at most 5 uops/cycle (vs 6 from DSB)."
        ),
        "typical_causes": [
            "Code not fitting in the 1536-entry DSB (uop cache)",
            "32-byte aligned regions exceeding 6-uop DSB line capacity",
            "I-cache misses forcing re-decode from L2",
            "Code regions evicted from DSB by other hot regions",
        ],
        "tuning_hints": [
            "Improve DSB coverage with PGO/BOLT to keep hot code compact",
            "Avoid instruction sequences that exceed 6 uops per 32-byte window",
            "Reduce hot code footprint to fit within DSB capacity",
            "For Redis, keep the main event loop and command handlers DSB-resident",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Bandwidth",
    },

    "Frontend_Bound.Fetch_Bandwidth.DSB": {
        "description": (
            "Fraction of uops delivered by the DSB (Decoded Stream Buffer / uop cache).  "
            "Higher is better as DSB can deliver up to 6 uops/cycle with lower latency."
        ),
        "typical_causes": [
            "Good code locality with hot paths fitting in DSB (positive indicator)",
            "PGO/BOLT-optimized binaries typically show high DSB delivery",
        ],
        "tuning_hints": [
            "High DSB is desirable -- no action needed unless total Frontend_Bound is high",
            "If DSB is high but Fetch_Bandwidth is also high, check for DSB line fragmentation",
            "Maintain current code layout optimizations",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Bandwidth",
    },

    "Frontend_Bound.Fetch_Bandwidth.MS": {
        "description": (
            "Fraction of uops delivered by the microcode sequencer (MS).  "
            "These are multi-uop instructions that must be fetched from microcode ROM."
        ),
        "typical_causes": [
            "REP-prefixed string operations (REP MOVSB, REP STOSB, REP CMPSB)",
            "Complex CISC instructions (ENTER, LEAVE, PUSHA)",
            "Microcode assists for special conditions (denormals, etc.)",
        ],
        "tuning_hints": [
            "Replace REP string ops with optimized library calls for known sizes",
            "Avoid complex CISC instructions in hot code; prefer simple RISC-like sequences",
            "Check for microcode assists contributing to MS delivery",
            "For Redis, audit sdscat/sdscpy paths for compiler-generated REP sequences",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Bandwidth",
    },

    "Frontend_Bound.Fetch_Bandwidth.LSD": {
        "description": (
            "Fraction of uops delivered by the Loop Stream Detector (LSD).  "
            "The LSD can replay small loops without re-fetching or re-decoding.  "
            "Available on SKL, ICL, and PTL (disabled on some steppings)."
        ),
        "typical_causes": [
            "Tight inner loops fitting within LSD capacity (~64 uops)",
            "LSD active indicates efficient loop execution (positive indicator)",
        ],
        "tuning_hints": [
            "High LSD delivery is desirable for tight loops -- no action needed",
            "Keep hot inner loops under ~64 uops to benefit from LSD",
            "Note: LSD is disabled on some Skylake steppings due to errata",
            "On architectures without LSD, DSB serves the same purpose for loops",
        ],
        "level": 3,
        "parent": "Frontend_Bound.Fetch_Bandwidth",
    },

    # --- Branch_Mispredicts children ---

    "Bad_Speculation.Branch_Mispredicts.Cond_NT_Mispredicts": {
        "description": (
            "Mispredicted conditional branches that were predicted taken but "
            "actually not-taken.  The processor fetched down the wrong path."
        ),
        "typical_causes": [
            "Error-checking branches that are almost never taken",
            "Loop exit conditions mispredicted on the last iteration",
            "Infrequent failure paths in try/catch or NULL-check code",
        ],
        "tuning_hints": [
            "Use __builtin_expect to hint branches as unlikely",
            "Move unlikely branches out of line with __attribute__((cold))",
            "For Redis, mark error paths with serverAssert or unlikely() macros",
            "This is normal for loop exit branches -- often unavoidable",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Branch_Mispredicts",
    },

    "Bad_Speculation.Branch_Mispredicts.Cond_TK_Mispredicts": {
        "description": (
            "Mispredicted conditional branches that were predicted not-taken "
            "but actually taken.  Common when entering rarely-executed paths."
        ),
        "typical_causes": [
            "Branches entering error handlers or slow paths",
            "Data-dependent comparisons with irregular patterns",
            "Phase changes in workload causing predictor re-training",
        ],
        "tuning_hints": [
            "Use __builtin_expect to hint likely/unlikely directions",
            "Consider branchless alternatives for data-dependent comparisons",
            "For Redis, sort command lookup tables by frequency to improve prediction",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Branch_Mispredicts",
    },

    "Bad_Speculation.Branch_Mispredicts.Ind_Call_Mispredicts": {
        "description": (
            "Mispredicted indirect calls (CALL through register or memory).  "
            "The indirect target predictor (ITB/BTB) chose the wrong target."
        ),
        "typical_causes": [
            "Virtual function calls with many implementations",
            "Function pointer dispatch tables with varying targets",
            "Callback-heavy event-driven architectures",
        ],
        "tuning_hints": [
            "Devirtualize hot indirect calls when only one target is common",
            "Use switch/case instead of function-pointer tables in hot paths",
            "In Redis, reduce virtual dispatch in module API callbacks if possible",
            "Consider retpoline implications for Spectre mitigations",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Branch_Mispredicts",
    },

    "Bad_Speculation.Branch_Mispredicts.Ind_Jump_Mispredicts": {
        "description": (
            "Mispredicted indirect jumps (JMP through register or memory).  "
            "Common with computed goto and large switch statements."
        ),
        "typical_causes": [
            "Computed goto in interpreters or dispatch loops",
            "Large switch statements compiled to jump tables",
            "Dynamic dispatch with many targets",
        ],
        "tuning_hints": [
            "Reduce number of indirect jump targets by splitting dispatch tables",
            "Use PGO to train the compiler to generate better branch sequences",
            "For Redis Lua interpreter, consider threaded code optimizations",
            "Profile which targets are most common and add direct checks before indirect jump",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Branch_Mispredicts",
    },

    "Bad_Speculation.Branch_Mispredicts.Ret_Mispredicts": {
        "description": (
            "Mispredicted return instructions (RET).  The Return Stack Buffer (RSB) "
            "predicted the wrong return address."
        ),
        "typical_causes": [
            "RSB underflow from deep call stacks exceeding RSB depth (~16 entries)",
            "Mismatched CALL/RET pairs (e.g., CALL then JMP instead of RET)",
            "Context switches or interrupts corrupting RSB state",
            "Spectre v2 mitigations (retpoline) disrupting RSB",
        ],
        "tuning_hints": [
            "Keep hot call chains shallow (< 16 levels) to stay within RSB",
            "Avoid longjmp or setjmp in hot paths as they break RSB prediction",
            "Ensure CALL/RET are properly paired (avoid manual stack manipulation)",
            "This is usually low; if elevated, check for RSB-related Spectre mitigations",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Branch_Mispredicts",
    },

    "Bad_Speculation.Branch_Mispredicts.Other_Mispredicts": {
        "description": (
            "Mispredictions from branch types not covered by the specific "
            "sub-categories (direct unconditional branches that still cause "
            "resteer delays, etc.)."
        ),
        "typical_causes": [
            "Rare edge cases in branch prediction hardware",
            "First-time execution of new branches not yet in BTB",
        ],
        "tuning_hints": [
            "Usually negligible; focus on Cond and Indirect mispredicts first",
            "If elevated, check for unusual branch patterns or self-modifying code",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Branch_Mispredicts",
    },

    # --- Machine_Clears children ---

    "Bad_Speculation.Machine_Clears.Other_Nukes": {
        "description": (
            "Machine clears from causes other than memory ordering violations -- "
            "includes SMC (self-modifying code) clears, assist-triggered nukes, "
            "and other rare pipeline flushes."
        ),
        "typical_causes": [
            "Self-modifying code (JIT compilation, dynamic patching)",
            "Floating-point or SIMD assists (denormals, NaN operations)",
            "Microcode update triggers",
            "Performance monitoring interrupts (PMI) in unlucky timing",
        ],
        "tuning_hints": [
            "If JIT-related, batch code modifications and use proper cache invalidation",
            "Flush denormals to zero: _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON)",
            "Avoid writing to executable code pages during hot execution",
            "For Redis modules using JIT (e.g., Lua), minimize code regeneration frequency",
        ],
        "level": 3,
        "parent": "Bad_Speculation.Machine_Clears",
    },

    # --- Memory_Bound children ---

    "Backend_Bound.Memory_Bound.L1_Bound": {
        "description": (
            "Stalls caused by loads hitting in the L1 data cache but still "
            "experiencing latency, or by L1-related issues like store forwarding "
            "blocks, split loads, and DTLB misses."
        ),
        "typical_causes": [
            "Store-to-load forwarding failures (size/alignment mismatch)",
            "L1 DTLB misses requiring STLB lookup",
            "Cache line split loads (unaligned access spanning two cache lines)",
            "Lock contention on atomic operations",
            "4K aliasing (loads aliasing with stores in different pages)",
        ],
        "tuning_hints": [
            "Align data structures to cache line boundaries (64 bytes)",
            "Avoid overlapping stores and loads with different sizes to the same address",
            "Use _mm_prefetch for pointer-chasing patterns in Redis dict iteration",
            "Reduce LOCK-prefixed operations on shared data; batch updates",
            "Drill into DTLB_Load, Store_Fwd_Blk, Split_Loads for specific causes",
        ],
        "level": 3,
        "parent": "Backend_Bound.Memory_Bound",
    },

    "Backend_Bound.Memory_Bound.L2_Bound": {
        "description": (
            "Stalls caused by loads missing the L1 data cache and hitting in "
            "the L2 cache.  L2 hit latency is typically 12-14 cycles."
        ),
        "typical_causes": [
            "Working set exceeding L1D capacity (32-48 KB)",
            "Irregular access patterns defeating L1 prefetcher",
            "Moderate data structure sizes that fit in L2 but not L1",
        ],
        "tuning_hints": [
            "Improve data locality: pack hot fields together in structs",
            "Use cache-oblivious algorithms for data processing",
            "For Redis, consider compact encodings (ziplist/listpack) for small collections",
            "Software prefetch to L1 can help if access pattern is predictable",
            "Tile/block loops to improve L1 reuse before moving to next chunk",
        ],
        "level": 3,
        "parent": "Backend_Bound.Memory_Bound",
    },

    "Backend_Bound.Memory_Bound.L3_Bound": {
        "description": (
            "Stalls caused by loads missing L2 and hitting in the L3 (LLC) cache.  "
            "L3 hit latency is typically 30-50 cycles depending on the slice."
        ),
        "typical_causes": [
            "Working set exceeding L2 capacity (256 KB - 2 MB per core)",
            "Streaming access patterns evicting data from L2",
            "Cross-core sharing causing L3 lookups (snoops)",
            "Large hash tables or B-trees with random access",
        ],
        "tuning_hints": [
            "Increase LLC allocation with Intel RDT/CAT if available",
            "For Redis, size hash tables and skip lists to fit within L2 per-core budget",
            "Use software prefetch with sufficient lead time (~100 cycles ahead)",
            "Consider data structure compaction: embed small values instead of pointer indirection",
            "Drill into Contested_Accesses and Data_Sharing for cross-core effects",
        ],
        "level": 3,
        "parent": "Backend_Bound.Memory_Bound",
    },

    "Backend_Bound.Memory_Bound.DRAM_Bound": {
        "description": (
            "Stalls caused by loads missing all cache levels and going to main "
            "memory (DRAM).  Latency is typically 60-120ns (local) or 150-300ns "
            "(remote NUMA)."
        ),
        "typical_causes": [
            "Working set exceeding LLC capacity",
            "Random access to large data structures (hash tables, trees)",
            "Pointer-chasing through linked structures with poor spatial locality",
            "NUMA remote memory accesses",
            "Memory bandwidth saturation under heavy load",
        ],
        "tuning_hints": [
            "Use `numactl --membind=<node>` to ensure Redis data stays local",
            "Configure transparent hugepages (THP) for large Redis instances to reduce TLB misses",
            "Consider redis-server with jemalloc arena binding to local NUMA node",
            "Add software prefetch for pointer-chasing: prefetch next node while processing current",
            "Drill into MEM_Bandwidth vs MEM_Latency to distinguish BW vs latency bottleneck",
            "For multi-threaded Redis (io-threads), pin threads to same NUMA node as data",
        ],
        "level": 3,
        "parent": "Backend_Bound.Memory_Bound",
    },

    "Backend_Bound.Memory_Bound.CXL_Mem_Bound": {
        "description": (
            "Stalls caused by loads going to CXL-attached memory.  CXL memory "
            "has higher latency than local DRAM (typically 200-400ns).  "
            "Available on Sapphire Rapids (SPR) and Granite Rapids (GNR) platforms."
        ),
        "typical_causes": [
            "Data placed on CXL-attached memory tiers",
            "OS NUMA balancing migrating pages to CXL nodes",
            "Explicit CXL memory tiering policies placing cold data on CXL",
        ],
        "tuning_hints": [
            "Use `numactl` or mbind() to control CXL vs local DRAM placement",
            "Keep Redis hot data on local DRAM; use CXL for cold/bulk storage only",
            "Monitor CXL bandwidth utilization with PCM or perf uncore counters",
            "Consider transparent memory tiering (kernel NUMA demotion) to auto-tier cold pages to CXL",
            "Disable NUMA balancing if CXL latency is causing performance issues: sysctl vm.numa_balancing=0",
        ],
        "level": 3,
        "parent": "Backend_Bound.Memory_Bound",
    },

    "Backend_Bound.Memory_Bound.Store_Bound": {
        "description": (
            "Stalls caused by store operations -- store buffer full, cache miss "
            "on store, or store-related bandwidth limitations."
        ),
        "typical_causes": [
            "Store buffer full from burst of stores without intervening loads",
            "Non-temporal stores not being used for streaming writes",
            "False sharing between threads writing to adjacent cache lines",
            "Split stores crossing cache line boundaries",
        ],
        "tuning_hints": [
            "Use non-temporal stores (movntdq) for large sequential writes",
            "Align store targets to cache line boundaries to avoid splits",
            "Pad thread-local data to prevent false sharing (alignas(64))",
            "For Redis RDB/AOF writes, use large aligned buffers to amortize store overhead",
            "Drill into Store_Latency, False_Sharing, Split_Stores for specifics",
        ],
        "level": 3,
        "parent": "Backend_Bound.Memory_Bound",
    },

    # --- Core_Bound children ---

    "Backend_Bound.Core_Bound.Divider": {
        "description": (
            "Stalls caused by the divider unit being busy.  Integer and "
            "floating-point divides have long latency (20-90+ cycles) and the "
            "divider is not fully pipelined."
        ),
        "typical_causes": [
            "Frequent integer divides (modulo operations, hash computation)",
            "Floating-point divides or square roots",
            "Division-heavy algorithms (normalization, averaging)",
        ],
        "tuning_hints": [
            "Replace modulo with bitwise AND for power-of-2 divisors (x & (n-1))",
            "Use multiplicative inverse for constant divisors (compiler usually does this)",
            "For Redis hash table sizing, ensure sizes are powers of 2 for mask-based modulo",
            "Batch divisions or use lookup tables for repeated divisors",
            "Consider reciprocal approximation for FP divides where precision allows",
        ],
        "level": 3,
        "parent": "Backend_Bound.Core_Bound",
    },

    "Backend_Bound.Core_Bound.Serializing_Operation": {
        "description": (
            "Stalls caused by serializing operations that force the pipeline to "
            "drain before continuing.  Includes CPUID, certain MSR accesses, "
            "memory fences, and C-state transitions."
        ),
        "typical_causes": [
            "Frequent MFENCE/SFENCE/LFENCE instructions",
            "LOCK-prefixed operations on contended cache lines",
            "CPUID calls in hot paths (e.g., for feature detection)",
            "C-state wake-up latency from deep sleep states",
        ],
        "tuning_hints": [
            "Cache CPUID results at startup; never call in hot paths",
            "Replace MFENCE with lighter barriers (SFENCE, or compiler barriers) where safe",
            "For Redis, use lock-free data structures where possible to avoid LOCK prefix overhead",
            "Tune C-state latency limits: set /dev/cpu_dma_latency to prevent deep C-states",
            "Drill into Slow_Pause, C01_Wait, C02_Wait, Memory_Fence for specifics",
        ],
        "level": 3,
        "parent": "Backend_Bound.Core_Bound",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization": {
        "description": (
            "Stalls related to execution port utilization -- cycles where the "
            "backend has uops ready but ports are oversubscribed or underutilized "
            "due to dependency chains."
        ),
        "typical_causes": [
            "Long dependency chains serializing execution (Ports_Utilized_1)",
            "All ports busy but throughput-limited (Ports_Utilized_3m)",
            "No uops ready due to cache miss or other stalls (Ports_Utilized_0)",
            "Imbalanced port usage (e.g., all ALU ops on port 0/1, none on port 6)",
        ],
        "tuning_hints": [
            "Drill into Ports_Utilized_0/1/2/3m to classify the bottleneck",
            "For dependency chains (Utilized_1): break chains with extra accumulators",
            "For port saturation (Utilized_3m): check ALU vs Load vs Store balance",
            "Use `-march=native` to let compiler choose optimal port-balanced instruction sequences",
            "For Redis, unroll critical loops to expose more ILP",
        ],
        "level": 3,
        "parent": "Backend_Bound.Core_Bound",
    },

    # --- Light_Operations children ---

    "Retiring.Light_Operations.FP_Arith": {
        "description": (
            "Fraction of retiring slots used by floating-point arithmetic "
            "instructions (scalar and vector FP add, multiply, FMA, etc.)."
        ),
        "typical_causes": [
            "Floating-point math in scoring, ranking, or ML inference",
            "Scientific computation or statistics gathering",
            "Floating-point hash functions or random number generators",
        ],
        "tuning_hints": [
            "Check FP_Scalar vs FP_Vector: vectorize scalar FP code for higher throughput",
            "Use FMA (fused multiply-add) instructions where possible (-mfma flag)",
            "For Redis Streams or TimeSeries modules, vectorize aggregation functions",
            "Consider fixed-point arithmetic if precision requirements allow",
        ],
        "level": 3,
        "parent": "Retiring.Light_Operations",
    },

    "Retiring.Light_Operations.Int_Operations": {
        "description": (
            "Fraction of retiring slots used by integer arithmetic and logic "
            "operations (add, sub, and, or, shift, etc.)."
        ),
        "typical_causes": [
            "General integer computation (pointer arithmetic, counters, comparisons)",
            "Hash function computation",
            "Encoding/decoding operations (base64, CRC, etc.)",
        ],
        "tuning_hints": [
            "Check if SIMD integer operations could accelerate hot paths",
            "For Redis, consider SSE4.2 CRC32 instructions for hash computation",
            "High Int_Operations is normal for general-purpose code like Redis",
            "Look at Int_Vector sub-nodes for vectorization opportunities",
        ],
        "level": 3,
        "parent": "Retiring.Light_Operations",
    },

    "Retiring.Light_Operations.Memory_Operations": {
        "description": (
            "Fraction of retiring slots used by memory operations (loads and "
            "stores).  High values indicate data-movement-intensive code."
        ),
        "typical_causes": [
            "Memcpy/memmove-heavy code paths",
            "Frequent struct field accesses and pointer dereferencing",
            "Serialization/deserialization of data structures",
            "RDB save or AOF rewrite operations in Redis",
        ],
        "tuning_hints": [
            "Reduce unnecessary data movement; operate on data in-place when possible",
            "Use register-local variables for frequently accessed fields",
            "For Redis, avoid redundant copies in command processing pipeline",
            "Consider memory-mapped I/O (io_uring) to reduce explicit load/store overhead",
        ],
        "level": 3,
        "parent": "Retiring.Light_Operations",
    },

    "Retiring.Light_Operations.Fused_Instructions": {
        "description": (
            "Fraction of retiring slots used by macro-fused instructions "
            "(e.g., CMP+JCC or TEST+JCC fused into a single uop by the decoder)."
        ),
        "typical_causes": [
            "Branch-heavy code with compare-and-branch patterns",
            "Well-optimized code taking advantage of macro-fusion (good)",
        ],
        "tuning_hints": [
            "High Fused_Instructions is generally positive (efficient decode)",
            "Ensure compiler generates fusable CMP/TEST+JCC sequences (modern GCC/Clang do this)",
            "Avoid inserting instructions between CMP and JCC that break fusion",
        ],
        "level": 3,
        "parent": "Retiring.Light_Operations",
    },

    "Retiring.Light_Operations.Non_Fused_Branches": {
        "description": (
            "Fraction of retiring slots used by branch instructions that were "
            "not macro-fused with a preceding comparison."
        ),
        "typical_causes": [
            "Unconditional branches (JMP)",
            "Indirect branches that cannot be fused",
            "Branches separated from their comparison by intervening instructions",
        ],
        "tuning_hints": [
            "Minimize unconditional jumps by improving code layout",
            "Use PGO to let the compiler arrange basic blocks to reduce branches",
            "High Non_Fused_Branches may indicate suboptimal code layout",
        ],
        "level": 3,
        "parent": "Retiring.Light_Operations",
    },

    "Retiring.Light_Operations.Other_Light_Ops": {
        "description": (
            "Fraction of retiring slots used by other single-uop instructions "
            "not categorized elsewhere (NOPs, shuffles, moves, etc.)."
        ),
        "typical_causes": [
            "NOP padding for alignment",
            "Register-to-register moves (though many are eliminated by move elimination)",
            "Miscellaneous single-uop instructions",
        ],
        "tuning_hints": [
            "Check Nop_Instructions sub-node: excessive NOPs waste decode bandwidth",
            "High Other_Light_Ops is usually benign",
            "Verify compiler is not inserting excessive alignment NOPs in hot loops",
        ],
        "level": 3,
        "parent": "Retiring.Light_Operations",
    },

    # --- Heavy_Operations children ---

    "Retiring.Heavy_Operations.Few_Uops_Instructions": {
        "description": (
            "Fraction of retiring slots used by instructions that decode to "
            "2-3 micro-ops (not microcode-sequenced, but heavier than single-uop)."
        ),
        "typical_causes": [
            "CISC instructions with memory source operand (ADD [mem], reg)",
            "Read-modify-write instructions",
            "Some SIMD instructions that require multiple uops",
        ],
        "tuning_hints": [
            "Consider splitting memory-operand instructions into load + compute in hot loops",
            "Usually not a major concern unless very high",
            "Compiler optimization level -O2/-O3 generally handles this well",
        ],
        "level": 3,
        "parent": "Retiring.Heavy_Operations",
    },

    "Retiring.Heavy_Operations.Microcode_Sequencer": {
        "description": (
            "Fraction of retiring slots used by microcode-sequenced instructions "
            "(4+ uops from the microcode ROM).  These are the heaviest instructions."
        ),
        "typical_causes": [
            "REP-prefixed string operations (REP MOVSB, REP STOSB)",
            "CPUID, XSAVE, XRSTOR instructions",
            "Microcode assists (denormal FP, page walks with accessed/dirty bit updates)",
            "Complex x87 FP instructions (FSIN, FCOS, FPATAN)",
        ],
        "tuning_hints": [
            "Replace REP MOVSB with optimized memcpy for small constant sizes",
            "Avoid x87 FP; use SSE/AVX scalar FP instead",
            "Check Assists sub-node for assist-driven microcode overhead",
            "For Redis, verify glibc memcpy/memmove is using optimal ERMS/FSRM path",
            "Minimize XSAVE/XRSTOR by reducing context switch frequency",
        ],
        "level": 3,
        "parent": "Retiring.Heavy_Operations",
    },

    # ===================================================================
    # LEVEL 4
    # ===================================================================

    # --- ICache_Misses children ---

    "Frontend_Bound.Fetch_Latency.ICache_Misses.Code_L2_Hit": {
        "description": (
            "I-cache misses that hit in the L2 cache.  The instruction fetch "
            "latency is the L2 access time (~12-14 cycles)."
        ),
        "typical_causes": [
            "Code working set slightly exceeding L1I capacity (32 KB)",
            "Moderate code footprint with occasional cold code execution",
        ],
        "tuning_hints": [
            "Compact hot code to fit within L1I (32 KB) using PGO/BOLT",
            "Separate hot and cold code sections to maximize L1I utilization",
            "For Redis, inline critical small functions and outline cold error paths",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.ICache_Misses",
    },

    "Frontend_Bound.Fetch_Latency.ICache_Misses.Code_L2_Miss": {
        "description": (
            "I-cache misses that also miss in L2, requiring fetch from L3 or "
            "beyond.  This incurs 30-100+ cycle fetch latency."
        ),
        "typical_causes": [
            "Very large code footprint exceeding L2 capacity",
            "Many shared libraries with scattered hot code",
            "Dynamic loading of modules or JIT code from distant memory regions",
        ],
        "tuning_hints": [
            "Aggressively reduce code size: LTO, dead code elimination, -Os for cold code",
            "Use BOLT to colocate hot code within L2-resident range",
            "Reduce shared library count; statically link critical libraries",
            "For Redis, consider static linking of modules for hot-path performance",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.ICache_Misses",
    },

    # --- ITLB_Misses children ---

    "Frontend_Bound.Fetch_Latency.ITLB_Misses.Code_STLB_Hit": {
        "description": (
            "ITLB misses that hit in the second-level TLB (STLB).  "
            "STLB hit latency is ~7-9 cycles."
        ),
        "typical_causes": [
            "Code spanning more pages than ITLB capacity (~128 entries for 4 KB pages)",
            "Moderate code footprint with good STLB coverage",
        ],
        "tuning_hints": [
            "Consider 2 MB hugepages for code to reduce ITLB pressure",
            "Compact hot code into fewer pages using PGO/BOLT",
            "Verify with `perf stat -e itlb_misses.stlb_hit` to quantify",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.ITLB_Misses",
    },

    "Frontend_Bound.Fetch_Latency.ITLB_Misses.Code_STLB_Miss": {
        "description": (
            "ITLB misses that also miss in the STLB, requiring a full page walk.  "
            "Page walk latency is 20-100+ cycles depending on page table depth."
        ),
        "typical_causes": [
            "Very large code footprint exceeding STLB coverage",
            "Code spread across many memory regions (shared libraries, JIT, modules)",
            "4 KB pages with thousands of active code pages",
        ],
        "tuning_hints": [
            "Use 2 MB hugepages for code: compile with `-Wl,-zmax-page-size=2097152`",
            "Enable transparent hugepages for code mappings (may need kernel config)",
            "Reduce total number of code pages with aggressive dead code elimination",
            "For Redis, use `-Wl,-zcommon-page-size=2097152` to align text segment to hugepages",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.ITLB_Misses",
    },

    # --- Branch_Resteers children ---

    "Frontend_Bound.Fetch_Latency.Branch_Resteers.Mispredicts_Resteers": {
        "description": (
            "Frontend resteers caused specifically by branch mispredictions.  "
            "The fetch unit redirected after discovering a wrong-path fetch."
        ),
        "typical_causes": [
            "High branch misprediction rate on conditional or indirect branches",
            "Data-dependent branches with unpredictable patterns",
        ],
        "tuning_hints": [
            "See Branch_Mispredicts tuning hints for detailed remediation",
            "Use branchless coding techniques in hot paths",
            "Profile specific mispredicting branches with perf's branch profiling",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.Branch_Resteers",
    },

    "Frontend_Bound.Fetch_Latency.Branch_Resteers.Clears_Resteers": {
        "description": (
            "Frontend resteers caused by machine clears (not branch mispredictions).  "
            "The full pipeline was flushed and fetch restarted."
        ),
        "typical_causes": [
            "Memory ordering violations causing pipeline nuke",
            "Self-modifying code detection",
            "Floating-point or other microcode assists",
        ],
        "tuning_hints": [
            "See Machine_Clears tuning hints for remediation",
            "Avoid store-to-load forwarding violations",
            "Minimize self-modifying code patterns",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.Branch_Resteers",
    },

    "Frontend_Bound.Fetch_Latency.Branch_Resteers.Unknown_Branches": {
        "description": (
            "Frontend resteers caused by branches not found in the BTB "
            "(Branch Target Buffer).  First-time branches or BTB capacity misses."
        ),
        "typical_causes": [
            "Cold code being executed for the first time",
            "Very large number of unique branch sites exceeding BTB capacity",
            "JIT-compiled code with new branch targets",
        ],
        "tuning_hints": [
            "Reduce total number of unique branch sites in the working set",
            "Warm up code paths before benchmarking to populate BTB",
            "Use PGO to let compiler optimize branch layout for BTB efficiency",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Latency.Branch_Resteers",
    },

    # --- MITE children ---

    "Frontend_Bound.Fetch_Bandwidth.MITE.Decoder0_Alone": {
        "description": (
            "Cycles where only decoder 0 (the complex decoder) was active, "
            "while decoders 1-4 were idle.  This limits decode throughput to "
            "1 instruction per cycle."
        ),
        "typical_causes": [
            "Instructions exceeding simple decoder limits (>1 uop for decoders 1-4)",
            "Instruction length exceeding 8 bytes preventing parallel decode",
            "Prefix-heavy instructions blocking simple decoders",
        ],
        "tuning_hints": [
            "Use compiler flags to prefer shorter instruction encodings",
            "Avoid unnecessary REX/VEX prefixes in hand-written assembly",
            "This is usually a minor contributor; prioritize ICache and ITLB issues first",
        ],
        "level": 4,
        "parent": "Frontend_Bound.Fetch_Bandwidth.MITE",
    },

    # --- L1_Bound children ---

    "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load": {
        "description": (
            "Load stalls caused by data TLB misses.  The processor must walk "
            "the page table to translate the load virtual address."
        ),
        "typical_causes": [
            "Large data working set spanning many 4 KB pages",
            "Random access patterns touching many pages (hash tables, skip lists)",
            "Memory-mapped files with scattered access",
            "Transparent hugepage (THP) defragmentation overhead",
        ],
        "tuning_hints": [
            "Use 2 MB hugepages for Redis data: `echo always > /sys/kernel/mm/transparent_hugepage/enabled`",
            "For Redis, use explicit hugetlb pages for large allocations via jemalloc's `oversize_threshold`",
            "Improve data locality to touch fewer pages in hot paths",
            "Drill into Load_STLB_Hit vs Load_STLB_Miss for severity assessment",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.Store_Fwd_Blk": {
        "description": (
            "Stalls caused by store-to-load forwarding failures.  When a load "
            "tries to read data from a recent store, forwarding fails if "
            "sizes or alignments do not match."
        ),
        "typical_causes": [
            "Writing a 64-bit value then reading it back as two 32-bit halves",
            "Union/bitfield access where store and load sizes differ",
            "Misaligned stores followed by aligned loads (or vice versa)",
            "Compiler-generated type punning through memory",
        ],
        "tuning_hints": [
            "Ensure loads and stores to the same address use the same width",
            "Avoid type punning through memory; use memcpy for type conversion",
            "Align data structures to natural alignment boundaries",
            "In Redis, check SDS header access patterns for store-forwarding conflicts",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.L1_Latency_Dependency": {
        "description": (
            "Stalls caused by long dependency chains through L1 cache.  "
            "Pointer-chasing patterns where each load depends on the previous "
            "load's result create serial L1 access chains."
        ),
        "typical_causes": [
            "Pointer chasing (linked list traversal, tree walking)",
            "Dependent load chains in hash table probing",
            "Sequential field access through indirection pointers",
        ],
        "tuning_hints": [
            "Prefetch next pointer while processing current node: `__builtin_prefetch(node->next)`",
            "Flatten data structures: use arrays instead of linked lists where possible",
            "For Redis, consider listpack over quicklist for small lists to reduce indirection",
            "Interleave independent operations to break dependency chains",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.L1_Latency_Capacity": {
        "description": (
            "Stalls caused by L1 data cache capacity limitations leading to "
            "conflict misses even when the working set could theoretically fit.  "
            "Available on Panther Lake (PTL) and newer."
        ),
        "typical_causes": [
            "Hot data sets mapping to the same L1 cache sets (conflict misses)",
            "Strided access patterns with power-of-2 stride causing set thrashing",
            "Multiple arrays accessed in lockstep with aliasing addresses",
        ],
        "tuning_hints": [
            "Add padding to arrays to break power-of-2 stride aliasing",
            "Use cache-coloring techniques to distribute hot data across L1 sets",
            "Reorder struct fields to spread hot fields across different cache lines",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.Lock_Latency": {
        "description": (
            "Stalls caused by locked (atomic) memory operations.  LOCK-prefixed "
            "instructions must acquire exclusive cache line ownership and "
            "are serializing."
        ),
        "typical_causes": [
            "Contended atomic operations (atomic_inc, CAS) on shared counters",
            "Lock cmpxchg in mutex implementations under contention",
            "Reference counting on shared objects",
            "malloc/free lock contention in multi-threaded workloads",
        ],
        "tuning_hints": [
            "Use per-thread counters and aggregate periodically instead of shared atomics",
            "Reduce lock granularity or use lock-free algorithms",
            "For Redis io-threads, minimize shared state between threads",
            "Use jemalloc with per-thread arenas to reduce allocator lock contention",
            "Consider using thread-local storage (TLS) for frequently updated statistics",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.Split_Loads": {
        "description": (
            "Stalls caused by loads that cross a cache line boundary (64 bytes).  "
            "Split loads require two cache line accesses, doubling the latency."
        ),
        "typical_causes": [
            "Unaligned data structures with fields spanning cache line boundaries",
            "Packed structs without alignment padding",
            "Pointer arithmetic producing unaligned addresses",
        ],
        "tuning_hints": [
            "Align data structures to natural boundaries: `__attribute__((aligned(64)))`",
            "Ensure hot fields do not span cache line boundaries",
            "For Redis objects, verify robj and SDS headers are naturally aligned",
            "Use `perf stat -e ld_blocks.no_sr` to quantify split load frequency",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.Store_Early_Blk": {
        "description": (
            "Stalls caused by store address not being available early enough "
            "for store-to-load forwarding.  The load must wait for the store "
            "address to be computed.  Available on Panther Lake (PTL)."
        ),
        "typical_causes": [
            "Complex address computations delaying store address generation",
            "Dependent store addresses preventing early forwarding",
        ],
        "tuning_hints": [
            "Simplify store address computations in hot paths",
            "Pre-compute store addresses earlier in the instruction stream",
            "Usually a minor contributor; focus on larger bottlenecks first",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.FB_Full": {
        "description": (
            "Stalls caused by the fill buffer (line-fill buffer / LFB) being full.  "
            "All outstanding cache miss slots are occupied, blocking new misses."
        ),
        "typical_causes": [
            "Many concurrent L1 misses exceeding fill buffer capacity (~10-12 entries)",
            "Burst of cache misses from prefetch-unfriendly access patterns",
            "High MLP (memory-level parallelism) demand exceeding hardware capacity",
            "Long-latency misses (DRAM) holding fill buffers for extended periods",
        ],
        "tuning_hints": [
            "Reduce number of concurrent outstanding misses by improving cache hit rate",
            "Throttle software prefetch to avoid saturating fill buffers",
            "Improve memory access regularity so hardware prefetcher is effective",
            "For Redis, batch key lookups to amortize miss latency across requests",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound",
    },

    # --- L2_Bound children ---

    "Backend_Bound.Memory_Bound.L2_Bound.L2_Hit_Latency": {
        "description": (
            "Average latency impact of L2 cache hits.  Even though L2 is fast, "
            "many L2 hits can accumulate significant stall time."
        ),
        "typical_causes": [
            "Working set spilling from L1 to L2 but fitting in L2",
            "Streaming data patterns with L1-unfriendly stride",
            "High L2 hit rate but many total accesses",
        ],
        "tuning_hints": [
            "Improve L1 hit rate through data layout optimization (struct reordering)",
            "Use software prefetch to L1 for predictable access patterns",
            "Tile data processing to improve L1 temporal reuse",
            "For Redis, pack hot dict entry fields together to improve L1 coverage",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L2_Bound",
    },

    # --- L3_Bound children ---

    "Backend_Bound.Memory_Bound.L3_Bound.Contested_Accesses": {
        "description": (
            "Stalls caused by accessing cache lines that are in Modified or "
            "Exclusive state in another core's cache.  Requires cross-core snoop "
            "and data transfer."
        ),
        "typical_causes": [
            "True sharing of mutable data between cores",
            "Producer-consumer patterns on shared queues",
            "Shared counters or statistics updated by multiple threads",
            "Lock data structures bouncing between cores",
        ],
        "tuning_hints": [
            "Reduce inter-thread data sharing; use message passing instead of shared state",
            "Pad shared data to separate cache lines (avoid false sharing)",
            "For Redis io-threads, minimize sharing between main thread and IO threads",
            "Pin related threads to cores on the same physical core (hyperthreading) if sharing is unavoidable",
            "Use per-thread work queues instead of a single shared queue",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L3_Bound",
    },

    "Backend_Bound.Memory_Bound.L3_Bound.Data_Sharing": {
        "description": (
            "Stalls caused by accessing cache lines in Shared state on another "
            "core.  Less expensive than Contested_Accesses (data is clean) but "
            "still requires cross-core communication."
        ),
        "typical_causes": [
            "Multiple threads reading the same data (shared read-only structures)",
            "False sharing where reads intermix with writes on nearby addresses",
            "Read-heavy shared configuration data",
        ],
        "tuning_hints": [
            "Replicate read-only data per-thread or per-NUMA-node to avoid sharing",
            "Separate read-mostly data from write-frequently data into different cache lines",
            "For Redis, use thread-local copies of frequently read configuration values",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L3_Bound",
    },

    "Backend_Bound.Memory_Bound.L3_Bound.L3_Hit_Latency": {
        "description": (
            "Average latency of L3 cache hits.  L3 is a shared resource and "
            "hit latency varies by slice distance (30-50 cycles typical)."
        ),
        "typical_causes": [
            "Working set in the L2-to-L3 gap",
            "Random access patterns defeating L2 prefetcher",
            "Large data structures with moderate locality",
        ],
        "tuning_hints": [
            "Use Intel CAT (Cache Allocation Technology) to reserve LLC for Redis process",
            "Prefetch to L2 with `_mm_prefetch(addr, _MM_HINT_T1)` for predictable patterns",
            "Reduce working set size through data compression or compact encodings",
            "For Redis, consider increasing hash-max-ziplist-entries to keep more data compact",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L3_Bound",
    },

    "Backend_Bound.Memory_Bound.L3_Bound.SQ_Full": {
        "description": (
            "Stalls caused by the superqueue (SQ) / offcore request queue being full.  "
            "All outstanding L2 miss slots are occupied."
        ),
        "typical_causes": [
            "Very high rate of L2 misses exceeding offcore bandwidth",
            "Many concurrent memory requests from prefetchers and demand loads",
            "Bandwidth-limited workloads saturating memory subsystem",
        ],
        "tuning_hints": [
            "Reduce L2 miss rate through better data locality",
            "Throttle software prefetches if they are consuming SQ entries",
            "Consider disabling adjacent-line prefetcher if bandwidth is saturated",
            "For Redis, reduce working set or improve access locality to lower miss rate",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.L3_Bound",
    },

    # --- DRAM_Bound children ---

    "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Bandwidth": {
        "description": (
            "Stalls caused by memory bandwidth saturation.  The memory controllers "
            "cannot serve data fast enough even if latency were zero."
        ),
        "typical_causes": [
            "Streaming workloads with high bytes/instruction ratio",
            "Large sequential scans (SCAN command, RDB save) saturating channels",
            "Multiple cores competing for shared memory bandwidth",
            "Unbalanced NUMA memory traffic",
        ],
        "tuning_hints": [
            "Populate all memory channels for maximum bandwidth (6 or 8 channels per socket)",
            "Use non-temporal stores for streaming writes to avoid read-for-ownership",
            "Balance memory traffic across NUMA nodes with interleaved allocation",
            "For Redis SCAN operations, consider rate-limiting to avoid bandwidth saturation",
            "Use Intel Memory Bandwidth Monitoring (MBM) to identify bandwidth hogs",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.DRAM_Bound",
    },

    "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency": {
        "description": (
            "Stalls caused by memory access latency (not bandwidth).  The CPU is "
            "waiting for individual cache lines from DRAM with insufficient "
            "memory-level parallelism to hide the latency."
        ),
        "typical_causes": [
            "Pointer-chasing through large data structures in DRAM",
            "Dependent loads serializing memory accesses (no MLP)",
            "NUMA remote access adding 50-100ns extra latency",
            "Memory controller queue contention",
        ],
        "tuning_hints": [
            "Add software prefetch for pointer-chasing patterns",
            "Ensure NUMA locality: `numactl --cpubind=0 --membind=0 redis-server`",
            "Increase memory-level parallelism by unrolling/interleaving independent lookups",
            "Drill into Local_MEM vs Remote_MEM to identify NUMA issues",
            "For Redis dict resizing, prefetch multiple hash buckets ahead during rehashing",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.DRAM_Bound",
    },

    # --- Store_Bound children ---

    "Backend_Bound.Memory_Bound.Store_Bound.Store_Latency": {
        "description": (
            "Stalls caused by store operations waiting for cache line ownership.  "
            "Stores to lines not in the local cache require a Read-For-Ownership "
            "(RFO) request."
        ),
        "typical_causes": [
            "Stores to cold cache lines requiring RFO from LLC or DRAM",
            "Stores to remote NUMA memory",
            "Burst writes to scattered addresses",
        ],
        "tuning_hints": [
            "Use non-temporal stores (MOVNTI/MOVNTDQ) for write-only streaming patterns",
            "Prefetch with write intent: `_mm_prefetch(addr, _MM_HINT_ET0)` before stores",
            "Batch writes to the same cache line together",
            "For Redis AOF fsync, use buffered writes to reduce store traffic to mmap regions",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.False_Sharing": {
        "description": (
            "Stalls caused by false sharing -- different threads writing to "
            "different variables that reside on the same cache line, causing "
            "expensive cross-core coherence traffic."
        ),
        "typical_causes": [
            "Thread-local counters or flags packed into the same cache line",
            "Adjacent elements in shared arrays written by different threads",
            "Struct members updated by different threads without padding",
        ],
        "tuning_hints": [
            "Pad thread-private data to 64-byte (cache line) boundaries: `alignas(64)`",
            "Use `__attribute__((aligned(64)))` for per-thread structures",
            "In Redis, verify server.stat_* counters are not causing false sharing with IO threads",
            "Use `perf c2c` to identify specific false-sharing cache lines and variables",
            "Consider grouping per-thread state into a cache-line-aligned struct",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.Split_Stores": {
        "description": (
            "Stalls caused by stores crossing cache line boundaries.  "
            "Each split store requires two cache line accesses."
        ),
        "typical_causes": [
            "Unaligned writes to packed data structures",
            "String operations crossing cache line boundaries",
            "Serialization writing variable-length records without alignment",
        ],
        "tuning_hints": [
            "Align write targets to cache line boundaries where possible",
            "Avoid packed structs in hot write paths",
            "For Redis RDB serialization, align output buffer writes to avoid splits",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.Streaming_Stores": {
        "description": (
            "Fraction of stores using non-temporal (streaming) store instructions "
            "(MOVNTI, MOVNTDQ, etc.).  These bypass the cache hierarchy, writing "
            "directly to memory."
        ),
        "typical_causes": [
            "Explicit use of _mm_stream_* intrinsics",
            "Compiler-generated non-temporal stores for memset/memcpy of large regions",
            "Write-combining stores to WC memory regions",
        ],
        "tuning_hints": [
            "Non-temporal stores are appropriate for large sequential writes (>LLC size)",
            "Avoid NT stores for small or random writes (they evict the cache line)",
            "For Redis RDB background save, NT stores may help for large sequential dumps",
            "Ensure write-combining buffers are not saturated (check with uncore counters)",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store": {
        "description": (
            "Store stalls caused by data TLB misses on store addresses.  "
            "The processor must perform a page walk to translate the store target."
        ),
        "typical_causes": [
            "Stores to many distinct pages (large data sets, scattered writes)",
            "Write-heavy workloads with poor page locality",
            "First touch of new pages triggering TLB miss + page fault",
        ],
        "tuning_hints": [
            "Use 2 MB hugepages to dramatically reduce DTLB miss rate",
            "Pre-fault pages before use with madvise(MADV_POPULATE_WRITE)",
            "Improve write locality to touch fewer pages per time window",
            "Drill into Store_STLB_Hit/Miss for severity",
        ],
        "level": 4,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound",
    },

    # --- Divider children ---

    "Backend_Bound.Core_Bound.Divider.FP_Divider": {
        "description": (
            "Stalls caused by the floating-point divider unit.  FP divides "
            "have 11-22 cycle latency and are not fully pipelined."
        ),
        "typical_causes": [
            "Floating-point division in hot loops (normalization, averaging)",
            "FP square root operations (shares divider unit)",
            "Statistical computations (variance, standard deviation)",
        ],
        "tuning_hints": [
            "Use reciprocal approximation + Newton-Raphson refinement instead of division",
            "Multiply by precomputed inverse: `x * (1.0/y)` where y is loop-invariant",
            "For Redis TimeSeries aggregation, precompute divisors outside inner loops",
            "Use RCPSS/RCPPS for approximate reciprocal if precision allows",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Divider",
    },

    "Backend_Bound.Core_Bound.Divider.INT_Divider": {
        "description": (
            "Stalls caused by the integer divider unit.  Integer divides (IDIV/DIV) "
            "have 20-90+ cycle latency depending on operand width."
        ),
        "typical_causes": [
            "Modulo operations with non-power-of-2 divisors",
            "Hash function computations using division",
            "Converting between bases or formatting numbers",
        ],
        "tuning_hints": [
            "Replace modulo with bitwise AND for power-of-2 divisors: `x & (n-1)`",
            "Compiler typically replaces constant division with multiply+shift; verify with -O2",
            "For Redis, ensure hash table sizes are powers of 2 (they are by design)",
            "Use Barrett reduction or similar for repeated modulo with same divisor",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Divider",
    },

    # --- Serializing_Operation children ---

    "Backend_Bound.Core_Bound.Serializing_Operation.Slow_Pause": {
        "description": (
            "Stalls caused by PAUSE instructions with long latency.  On modern "
            "CPUs, PAUSE can stall ~140 cycles (up from ~10 on older CPUs)."
        ),
        "typical_causes": [
            "Spin-wait loops using PAUSE instruction",
            "Lock implementations with PAUSE in retry loops",
            "Busy-polling in event loops",
        ],
        "tuning_hints": [
            "Replace spin-waits with OS-level blocking (futex, epoll) for long waits",
            "Use adaptive spinning: spin briefly then yield/block",
            "For Redis, check if event loop spin-wait (if any) uses appropriate backoff",
            "On GNR/SPR, PAUSE has ~40 cycles; still significant in tight loops",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Serializing_Operation",
    },

    "Backend_Bound.Core_Bound.Serializing_Operation.C01_Wait": {
        "description": (
            "Stalls caused by transitioning from C0.1 power state.  The CPU was in "
            "a shallow idle state and took time to resume full execution."
        ),
        "typical_causes": [
            "Interrupt-driven workloads with idle gaps between events",
            "Low-utilization periods where CPU enters C0.1 state",
            "Aggressive C-state policy waking from shallow idle too often",
        ],
        "tuning_hints": [
            "Set CPU governor to 'performance' to reduce C-state transitions",
            "Use `/dev/cpu_dma_latency` to set maximum acceptable wake-up latency",
            "For latency-sensitive Redis, disable shallow C-states: `intel_idle.max_cstate=0`",
            "Use `tuned` profile `latency-performance` to minimize idle transitions",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Serializing_Operation",
    },

    "Backend_Bound.Core_Bound.Serializing_Operation.C02_Wait": {
        "description": (
            "Stalls caused by transitioning from C0.2 power state.  Deeper than "
            "C0.1, with correspondingly longer wake-up latency."
        ),
        "typical_causes": [
            "CPU entering deeper idle state during longer idle periods",
            "Insufficient workload to keep CPU continuously active",
        ],
        "tuning_hints": [
            "Same mitigations as C01_Wait but more critical",
            "Write 0 to `/dev/cpu_dma_latency` to prevent deep C-states",
            "Ensure Redis is pinned to dedicated cores that stay active",
            "Consider using `busy-polling` in Redis to keep CPU active during low traffic",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Serializing_Operation",
    },

    "Backend_Bound.Core_Bound.Serializing_Operation.Memory_Fence": {
        "description": (
            "Stalls caused by explicit memory fence instructions (MFENCE, SFENCE, "
            "LFENCE) or implicit fences from serializing instructions."
        ),
        "typical_causes": [
            "Explicit MFENCE/SFENCE in lock-free algorithms",
            "LFENCE inserted by compiler for Spectre mitigations",
            "LOCK-prefixed instructions acting as implicit fences",
        ],
        "tuning_hints": [
            "Replace MFENCE with SFENCE where only store ordering is needed",
            "Use compiler barriers (__atomic_signal_fence) instead of hardware fences where safe",
            "Reduce LFENCE overhead: consider retpoline alternatives or process-based isolation",
            "For Redis, audit atomics for unnecessary full barriers (use relaxed ordering where safe)",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Serializing_Operation",
    },

    # --- Ports_Utilization children ---

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_0": {
        "description": (
            "Cycles where no execution port was utilized despite uops being "
            "allocated.  This typically means all in-flight uops are waiting "
            "for memory or other long-latency operations."
        ),
        "typical_causes": [
            "Cache misses stalling all dependent uops",
            "Long-latency operations (divides) blocking dependent chains",
            "Resource stalls (ROB full, RS full) preventing execution",
        ],
        "tuning_hints": [
            "This metric often correlates with Memory_Bound -- fix memory issues first",
            "Increase instruction-level parallelism to have independent work available",
            "Check Mixing_Vectors sub-node for SSE/AVX transition penalties",
            "For Redis, reduce long dependency chains in key lookup paths",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_1": {
        "description": (
            "Cycles where exactly one execution port was utilized.  This indicates "
            "serialized execution, likely due to long dependency chains."
        ),
        "typical_causes": [
            "Long dependency chains: each instruction depends on the previous result",
            "Single-threaded execution of inherently serial algorithms",
            "Pointer chasing with computation between loads",
        ],
        "tuning_hints": [
            "Break dependency chains: use multiple accumulators in reduction loops",
            "Unroll loops to expose independent operations",
            "For Redis CRC or hash computations, use parallel accumulator technique",
            "Reorder independent instructions to allow out-of-order execution to overlap",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_2": {
        "description": (
            "Cycles where exactly two execution ports were utilized.  Moderate "
            "parallelism but below the 4+ uop/cycle potential of the backend."
        ),
        "typical_causes": [
            "Moderate dependency chains limiting parallelism",
            "Mixed load/compute code with partial serialization",
            "Instruction mix not utilizing all available ports",
        ],
        "tuning_hints": [
            "Look for opportunities to increase ILP through loop unrolling",
            "Ensure compiler is generating code that utilizes all relevant ports",
            "For Redis, interleave independent hash lookups to improve port utilization",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m": {
        "description": (
            "Cycles where three or more execution ports were utilized.  "
            "Good parallelism but may indicate port contention if backends stalls "
            "are also high."
        ),
        "typical_causes": [
            "High-ILP code saturating execution resources (positive if not stalling)",
            "Port contention when specific ports are oversubscribed",
            "Imbalanced instruction mix overloading ALU or Load/Store ports",
        ],
        "tuning_hints": [
            "Drill into ALU_Op_Utilization, Load_Op_Utilization, Store_Op_Utilization",
            "If ALU-heavy, check if SIMD can reduce instruction count",
            "If Load-heavy, combine loads by packing data more densely",
            "Use `-march=native` to let compiler balance port usage optimally",
        ],
        "level": 4,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization",
    },

    # --- FP_Arith children ---

    "Retiring.Light_Operations.FP_Arith.X87_Use": {
        "description": (
            "Fraction of retiring slots using legacy x87 floating-point instructions.  "
            "x87 is slower and less efficient than SSE/AVX scalar FP."
        ),
        "typical_causes": [
            "Legacy code compiled without SSE FP (-mfpmath=387)",
            "Long double (80-bit) arithmetic requiring x87",
            "Some transcendental functions (fsin, fcos) only in x87",
        ],
        "tuning_hints": [
            "Compile with `-mfpmath=sse` (default on x86-64) to avoid x87",
            "Replace x87 transcendentals with SSE/AVX library equivalents",
            "Audit third-party libraries or Redis modules for x87 usage",
            "x87 should be near-zero in modern 64-bit builds",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.FP_Arith",
    },

    "Retiring.Light_Operations.FP_Arith.FP_Scalar": {
        "description": (
            "Fraction of retiring slots using scalar SSE/AVX floating-point "
            "operations (ADDSS, MULSS, etc.).  Only one FP element processed per instruction."
        ),
        "typical_causes": [
            "Floating-point computations not vectorized by compiler",
            "Data-dependent FP operations that cannot be vectorized",
            "Single FP value manipulations (timestamps, scores)",
        ],
        "tuning_hints": [
            "Vectorize FP loops: use -O3 -ftree-vectorize -march=native",
            "Restructure data from AoS to SoA for SIMD-friendly access",
            "For Redis TimeSeries, batch aggregation computations for vectorization",
            "Use explicit SIMD intrinsics for critical FP computations",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.FP_Arith",
    },

    "Retiring.Light_Operations.FP_Arith.FP_Vector": {
        "description": (
            "Fraction of retiring slots using vector SSE/AVX floating-point "
            "operations (ADDPS, VFMADD*, etc.).  Multiple FP elements per instruction."
        ),
        "typical_causes": [
            "Auto-vectorized FP loops",
            "Explicit SIMD intrinsics (SSE/AVX/AVX-512)",
            "Vectorized math library calls",
        ],
        "tuning_hints": [
            "High FP_Vector is generally good -- indicates effective vectorization",
            "Check FP_Vector_128b vs FP_Vector_256b: prefer wider vectors for throughput",
            "Watch for AVX-512 frequency throttling on older CPUs (SKL/ICL)",
            "For Redis, vectorized string matching or JSON parsing can benefit from wider vectors",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.FP_Arith",
    },

    # --- Int_Operations children ---

    "Retiring.Light_Operations.Int_Operations.Int_Vector_128b": {
        "description": (
            "Fraction of retiring slots using 128-bit integer SIMD operations "
            "(SSE2/SSE4 integer instructions like PADDD, PCMPEQ, etc.)."
        ),
        "typical_causes": [
            "String processing with SSE4.2 (PCMPESTRI, PCMPISTRI)",
            "Auto-vectorized integer loops at 128-bit width",
            "CRC32 computation using SSE4.2 intrinsics",
        ],
        "tuning_hints": [
            "Consider widening to 256-bit (AVX2) if data volume justifies it",
            "For Redis, SSE4.2 string operations are good for command parsing",
            "Use 128-bit only when data set is small or 256-bit causes downclocking",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.Int_Operations",
    },

    "Retiring.Light_Operations.Int_Operations.Int_Vector_256b": {
        "description": (
            "Fraction of retiring slots using 256-bit integer SIMD operations "
            "(AVX2 integer instructions like VPADDD, VPAND, etc.)."
        ),
        "typical_causes": [
            "AVX2-vectorized integer loops",
            "String/buffer operations vectorized to 256-bit width",
            "Bitwise operations on large bitmaps",
        ],
        "tuning_hints": [
            "AVX2 is generally frequency-safe (no downclocking on modern CPUs)",
            "For Redis BITOP operations, AVX2 can provide significant speedup",
            "Ensure alignment to 32 bytes for AVX2 memory operands",
            "On GNR/SPR, AVX-512 may be better for integer ops without frequency penalty",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.Int_Operations",
    },

    # --- Other_Light_Ops children ---

    "Retiring.Light_Operations.Other_Light_Ops.Nop_Instructions": {
        "description": (
            "Fraction of retiring slots used by NOP instructions (including "
            "multi-byte NOPs used for alignment)."
        ),
        "typical_causes": [
            "Compiler-inserted alignment padding (loop alignment, function alignment)",
            "BOLT/AutoFDO inserting NOPs for layout optimization",
            "Debug builds with NOP sleds or breakpoint padding",
        ],
        "tuning_hints": [
            "Some NOP overhead is acceptable for alignment benefits",
            "If excessive, reduce alignment padding: `-falign-functions=16` instead of 32",
            "Verify debug NOPs are not present in production builds",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.Other_Light_Ops",
    },

    "Retiring.Light_Operations.Other_Light_Ops.Shuffles_256b": {
        "description": (
            "Fraction of retiring slots used by 256-bit shuffle/permute "
            "instructions (VPSHUFB, VPERMD, etc.)."
        ),
        "typical_causes": [
            "AVX2 data reorganization in vectorized code",
            "Look-up table implementations using VPSHUFB",
            "Data format conversion between SoA and AoS layouts",
        ],
        "tuning_hints": [
            "High shuffle overhead may indicate suboptimal data layout",
            "Consider SoA (Structure of Arrays) layout to avoid shuffles",
            "For Redis, if vectorized parsing requires many shuffles, reconsider algorithm",
        ],
        "level": 4,
        "parent": "Retiring.Light_Operations.Other_Light_Ops",
    },

    # --- Microcode_Sequencer children ---

    "Retiring.Heavy_Operations.Microcode_Sequencer.Assists": {
        "description": (
            "Fraction of retiring slots consumed by microcode assists -- special "
            "microcode routines triggered by exceptional conditions like page "
            "faults, FP denormals, or AVX-SSE transitions."
        ),
        "typical_causes": [
            "Page faults during memory access (first touch, copy-on-write)",
            "Floating-point denormal number operations",
            "AVX-to-SSE transition penalties (VEX/non-VEX mixing)",
            "Accessed/dirty bit updates in page table entries",
        ],
        "tuning_hints": [
            "Pre-fault memory with madvise(MADV_POPULATE_WRITE) before hot path",
            "Set denormals-to-zero mode for FP-heavy code",
            "Avoid mixing VEX (AVX) and non-VEX (SSE) code; use VZEROUPPER between",
            "Drill into Page_Faults, FP_Assists, AVX_Assists for specifics",
        ],
        "level": 4,
        "parent": "Retiring.Heavy_Operations.Microcode_Sequencer",
    },

    "Retiring.Heavy_Operations.Microcode_Sequencer.CISC": {
        "description": (
            "Fraction of retiring slots consumed by complex CISC instructions "
            "that are decoded into many micro-ops by the microcode sequencer."
        ),
        "typical_causes": [
            "REP-prefixed string operations (REP MOVSB, REP STOSB, REP SCASB)",
            "ENTER/LEAVE instructions for stack frame setup",
            "Complex BCD arithmetic instructions",
            "XSAVE/XRSTOR for extended register state save/restore",
        ],
        "tuning_hints": [
            "Replace REP-prefixed ops with optimized library calls for small sizes",
            "Compiler should avoid ENTER/LEAVE; verify with -O2 or higher",
            "XSAVE cost is proportional to state size; minimize AVX-512 state if unused",
            "For Redis, ensure memcpy/memset from glibc uses ERMS efficiently for large copies",
        ],
        "level": 4,
        "parent": "Retiring.Heavy_Operations.Microcode_Sequencer",
    },

    # ===================================================================
    # LEVEL 5
    # ===================================================================

    # --- Code_STLB_Miss children ---

    "Frontend_Bound.Fetch_Latency.ITLB_Misses.Code_STLB_Miss.Code_STLB_Miss_4K": {
        "description": (
            "ITLB STLB misses on 4 KB code pages, requiring a full 4-level page walk."
        ),
        "typical_causes": [
            "Large code footprint with thousands of active 4 KB code pages",
            "Code not eligible for or not mapped to hugepages",
        ],
        "tuning_hints": [
            "Map code with 2 MB hugepages to eliminate 4 KB page walks",
            "Reduce code page count by compacting hot code with BOLT",
            "Check `/proc/<pid>/smaps` for text segment page sizes",
        ],
        "level": 5,
        "parent": "Frontend_Bound.Fetch_Latency.ITLB_Misses.Code_STLB_Miss",
    },

    "Frontend_Bound.Fetch_Latency.ITLB_Misses.Code_STLB_Miss.Code_STLB_Miss_2M": {
        "description": (
            "ITLB STLB misses on 2 MB code pages.  Even with hugepages, the STLB "
            "can miss if code spans many 2 MB regions."
        ),
        "typical_causes": [
            "Extremely large code footprint exceeding STLB capacity even with 2 MB pages",
            "Many shared libraries each mapped to separate 2 MB pages",
        ],
        "tuning_hints": [
            "Reduce total code footprint; 2 MB pages should cover most Redis workloads",
            "Consolidate shared libraries; consider static linking for hot libraries",
            "Verify STLB is not being thrashed by other address space consumers",
        ],
        "level": 5,
        "parent": "Frontend_Bound.Fetch_Latency.ITLB_Misses.Code_STLB_Miss",
    },

    # --- DTLB_Load children ---

    "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Hit": {
        "description": (
            "Data TLB misses on loads that hit in the second-level TLB (STLB).  "
            "STLB hit adds ~7-9 cycles to the load latency."
        ),
        "typical_causes": [
            "Data working set spanning more pages than L1 DTLB capacity (~64 entries)",
            "Random access patterns touching many pages",
            "Hash table or tree lookups with scattered data",
        ],
        "tuning_hints": [
            "Use 2 MB hugepages to reduce TLB entry pressure by 512x",
            "Improve data locality to access fewer pages in hot paths",
            "For Redis, use compact data encodings to reduce memory footprint and page count",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss": {
        "description": (
            "Data TLB misses on loads that miss both L1 DTLB and STLB, requiring "
            "a full hardware page walk (20-100+ cycles)."
        ),
        "typical_causes": [
            "Very large data working set with many active pages",
            "Random access to data spread across thousands of pages",
            "STLB capacity exceeded by combined code + data page count",
        ],
        "tuning_hints": [
            "Use 2 MB or 1 GB hugepages for large Redis data sets",
            "Configure jemalloc to use hugetlb: `echo 'thp:always' > /proc/<pid>/jemalloc/opt.thp`",
            "Drill into Load_STLB_Miss_4K/2M/1G to see page-size distribution",
            "Consider `vm.nr_hugepages` or hugetlbfs for explicit hugepage allocation",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load",
    },

    # --- MEM_Latency children ---

    "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency.Local_MEM": {
        "description": (
            "Stalls caused by loads served from local NUMA node DRAM.  Typical "
            "latency is 60-100ns depending on memory technology and load."
        ),
        "typical_causes": [
            "Working set exceeding LLC with good NUMA locality",
            "Pointer-chasing through local DRAM-resident data",
            "Large sequential scan through local memory",
        ],
        "tuning_hints": [
            "This is the best-case DRAM scenario -- data is at least local",
            "Add software prefetch to hide local DRAM latency (~200 cycles)",
            "Consider higher-bandwidth memory (DDR5-5600 vs DDR5-4800) if BW-limited",
            "For Redis, local DRAM access is expected for large key spaces; focus on reducing miss rate",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency",
    },

    "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency.Remote_MEM": {
        "description": (
            "Stalls caused by loads served from remote NUMA node DRAM.  "
            "Remote access adds 50-150ns over local access via UPI/QPI interconnect."
        ),
        "typical_causes": [
            "Redis process accessing memory allocated on wrong NUMA node",
            "OS NUMA balancing migrating pages or not migrating them",
            "Multi-socket systems without NUMA-aware memory allocation",
            "fork() for RDB save inheriting pages on remote node",
        ],
        "tuning_hints": [
            "Use `numactl --membind=<node> --cpubind=<node>` for Redis",
            "Set `vm.zone_reclaim_mode=1` to prefer local allocation",
            "Monitor with `numastat -p <redis-pid>` for remote access counts",
            "For Redis Cluster, ensure each shard is bound to a single NUMA node",
            "Disable automatic NUMA balancing if it is causing remote accesses: `vm.numa_balancing=0`",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency",
    },

    "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency.Remote_Cache": {
        "description": (
            "Stalls caused by loads served from a remote socket's cache (snoop "
            "hit on remote LLC).  Faster than remote DRAM but slower than local cache."
        ),
        "typical_causes": [
            "Cross-socket data sharing between threads",
            "Migrated threads accessing data cached on previous socket",
            "Multi-socket Redis with shared state across sockets",
        ],
        "tuning_hints": [
            "Pin Redis threads to a single socket to avoid cross-socket cache access",
            "Replicate read-only shared data per socket",
            "For Redis Cluster, co-locate clients and shards on the same socket",
            "Use `perf stat -e offcore_response.demand_data_rd.l3_miss.remote_hitm` to quantify",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.DRAM_Bound.MEM_Latency",
    },

    # --- DTLB_Store children ---

    "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Hit": {
        "description": (
            "Store DTLB misses that hit in the STLB.  Adds ~7-9 cycles "
            "to the store commit path."
        ),
        "typical_causes": [
            "Write-heavy workloads touching many pages exceeding L1 DTLB capacity",
            "Scattered store patterns (log writes, stats updates across pages)",
        ],
        "tuning_hints": [
            "Use 2 MB hugepages to reduce DTLB pressure for stores",
            "Batch writes to the same page to improve DTLB temporal reuse",
            "For Redis AOF writes, use large aligned buffers to minimize page scatter",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss": {
        "description": (
            "Store DTLB misses that miss both L1 DTLB and STLB, requiring "
            "a full page walk for the store address translation."
        ),
        "typical_causes": [
            "Very large write footprint spanning thousands of pages",
            "First-touch writes to newly mapped memory",
            "Write-heavy workloads exceeding STLB capacity",
        ],
        "tuning_hints": [
            "Use hugepages (2 MB/1 GB) to reduce TLB miss rate",
            "Pre-fault memory pages before hot path: madvise(MADV_POPULATE_WRITE)",
            "Drill into Store_STLB_Miss_4K/2M/1G for page-size breakdown",
            "For Redis, pre-allocate and pre-fault memory pools at startup",
        ],
        "level": 5,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store",
    },

    # --- Ports_Utilized_0 children ---

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_0.Mixing_Vectors": {
        "description": (
            "Stalls caused by switching between different vector register widths "
            "(SSE 128-bit and AVX 256/512-bit).  On Skylake, this causes a penalty "
            "of ~70 cycles for the upper register save/restore."
        ),
        "typical_causes": [
            "Mixing SSE and AVX instructions without VZEROUPPER",
            "Calling SSE library code from AVX-enabled code",
            "Legacy libraries compiled without AVX awareness",
        ],
        "tuning_hints": [
            "Insert VZEROUPPER before calling non-AVX code from AVX context",
            "Compile all libraries with the same -march flag to avoid mixing",
            "Use `-mvzeroupper` compiler flag to auto-insert VZEROUPPER",
            "On ICL+ this penalty is eliminated but VZEROUPPER is still good practice",
            "For Redis modules, ensure consistent vector ISA across all compiled objects",
        ],
        "level": 5,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_0",
    },

    # --- Ports_Utilized_3m children ---

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization": {
        "description": (
            "Fraction of cycles where ALU (arithmetic/logic) execution ports are "
            "heavily utilized.  High values indicate compute-intensive code."
        ),
        "typical_causes": [
            "Compute-heavy code (hashing, CRC, encoding/decoding)",
            "Integer arithmetic loops without memory bottleneck",
            "SIMD compute kernels",
        ],
        "tuning_hints": [
            "Drill into Port_0, Port_1, Port_6 to see specific port pressure",
            "Balance ALU work across ports by varying instruction selection",
            "Use SIMD to reduce total instruction count for parallel-friendly computations",
            "For Redis, consider hardware-accelerated CRC32C via SSE4.2",
        ],
        "level": 5,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.Load_Op_Utilization": {
        "description": (
            "Fraction of cycles where load execution ports (port 2/3 on SKL, "
            "port 2/3/11 on GNR) are heavily utilized."
        ),
        "typical_causes": [
            "Code with very high load-to-instruction ratio",
            "Gather loads or many scalar loads from different addresses",
            "Data structure traversal with many pointer dereferences",
        ],
        "tuning_hints": [
            "Reduce load count by caching values in registers",
            "Combine small loads into larger loads (e.g., load 64-bit then extract fields)",
            "For Redis dict lookups, load the entire dictEntry in one wide load",
            "Use SIMD gather instructions cautiously (they consume multiple port cycles)",
        ],
        "level": 5,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.Store_Op_Utilization": {
        "description": (
            "Fraction of cycles where store execution ports (port 4/port 7 on SKL, "
            "port 4/9 on GNR) are heavily utilized."
        ),
        "typical_causes": [
            "Write-heavy code paths (serialization, buffer fills, memcpy)",
            "Store-intensive loops (clearing memory, initializing data structures)",
            "Many small scattered stores",
        ],
        "tuning_hints": [
            "Combine small stores into larger stores (e.g., store 64-bit instead of two 32-bit)",
            "Use non-temporal stores for streaming write patterns",
            "For Redis RDB save, use large buffered writes to reduce store port pressure",
            "On GNR with 3 store ports, this is less likely to be a bottleneck",
        ],
        "level": 5,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m",
    },

    # --- FP_Vector children ---

    "Retiring.Light_Operations.FP_Arith.FP_Vector.FP_Vector_128b": {
        "description": (
            "Fraction of retiring slots using 128-bit (SSE) vector floating-point "
            "operations.  Processes 2 doubles or 4 floats per instruction."
        ),
        "typical_causes": [
            "Auto-vectorized FP code at SSE width",
            "Explicit SSE2/SSE3 FP intrinsics",
            "Legacy code not recompiled for AVX",
        ],
        "tuning_hints": [
            "Consider recompiling with -mavx2 to use 256-bit FP vectors for 2x throughput",
            "Ensure data alignment to 16 bytes for SSE operations",
            "For Redis modules doing FP math, upgrade to AVX2 if target CPUs support it",
        ],
        "level": 5,
        "parent": "Retiring.Light_Operations.FP_Arith.FP_Vector",
    },

    "Retiring.Light_Operations.FP_Arith.FP_Vector.FP_Vector_256b": {
        "description": (
            "Fraction of retiring slots using 256-bit (AVX/AVX2) vector "
            "floating-point operations.  Processes 4 doubles or 8 floats per instruction."
        ),
        "typical_causes": [
            "AVX/AVX2 auto-vectorized FP loops",
            "Explicit AVX FP intrinsics (_mm256_*)",
            "Optimized math libraries using AVX",
        ],
        "tuning_hints": [
            "AVX-256 is generally the sweet spot: good throughput without frequency throttling",
            "On SPR/GNR, consider AVX-512 for FP without frequency penalty",
            "Ensure 32-byte alignment for AVX memory operands: `__attribute__((aligned(32)))`",
            "Monitor CPU frequency: some SKL steppings throttle for sustained AVX2 FP",
        ],
        "level": 5,
        "parent": "Retiring.Light_Operations.FP_Arith.FP_Vector",
    },

    # --- Assists children ---

    "Retiring.Heavy_Operations.Microcode_Sequencer.Assists.Page_Faults": {
        "description": (
            "Microcode assists triggered by page faults during execution.  "
            "Minor faults (page present but not accessed/dirty) and major faults "
            "(page not in memory) both trigger assists."
        ),
        "typical_causes": [
            "First touch of newly mmap'd memory (demand paging)",
            "Copy-on-write faults after fork() (Redis background save)",
            "Accessing swapped-out pages",
            "Transparent hugepage promotion/demotion",
        ],
        "tuning_hints": [
            "Pre-fault memory at startup: madvise(MADV_POPULATE_WRITE) or explicit touch",
            "For Redis, use `disable-thp yes` if THP compaction causes latency spikes",
            "Monitor major page faults: `perf stat -e major-faults` during steady state",
            "After fork() for RDB, expect CoW faults -- minimize writes to shared pages",
            "Use mlock() for latency-critical Redis data to prevent swap-out",
        ],
        "level": 5,
        "parent": "Retiring.Heavy_Operations.Microcode_Sequencer.Assists",
    },

    "Retiring.Heavy_Operations.Microcode_Sequencer.Assists.FP_Assists": {
        "description": (
            "Microcode assists triggered by floating-point exceptional conditions: "
            "denormal operands, underflow, overflow, or invalid operations."
        ),
        "typical_causes": [
            "Denormal (subnormal) FP numbers in computation",
            "Gradual underflow producing denormals",
            "Uninitialized FP variables with garbage bit patterns",
        ],
        "tuning_hints": [
            "Enable flush-to-zero and denormals-are-zero modes:",
            "  _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON);",
            "  _MM_SET_DENORMALS_ZERO_MODE(_MM_DENORMALS_ZERO_ON);",
            "Initialize all FP variables to avoid garbage denormals",
            "For Redis modules with FP math, set FTZ/DAZ at module init",
        ],
        "level": 5,
        "parent": "Retiring.Heavy_Operations.Microcode_Sequencer.Assists",
    },

    "Retiring.Heavy_Operations.Microcode_Sequencer.Assists.AVX_Assists": {
        "description": (
            "Microcode assists triggered by AVX-SSE transition penalties.  "
            "When upper halves of YMM registers contain non-zero data and "
            "SSE instructions are executed, a costly save/restore occurs."
        ),
        "typical_causes": [
            "Missing VZEROUPPER before SSE code after AVX code",
            "Mixing AVX and SSE libraries in the same call chain",
            "Context switches saving/restoring dirty upper YMM state",
        ],
        "tuning_hints": [
            "Always use VZEROUPPER after AVX code before calling non-AVX functions",
            "Compile with `-mvzeroupper` to auto-insert transitions",
            "Link all code with the same -march to avoid ISA mixing",
            "On Ice Lake and later, this penalty is eliminated but best practice remains",
        ],
        "level": 5,
        "parent": "Retiring.Heavy_Operations.Microcode_Sequencer.Assists",
    },

    # ===================================================================
    # LEVEL 6
    # ===================================================================

    # --- Load_STLB_Miss children ---

    "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss.Load_STLB_Miss_4K": {
        "description": (
            "Load STLB misses on 4 KB pages, requiring a full 4-level page table walk.  "
            "This is the most expensive TLB miss scenario."
        ),
        "typical_causes": [
            "Large data set on 4 KB pages exceeding STLB capacity",
            "Transparent hugepages not enabled or not covering the data region",
            "Many small mmap regions each on separate 4 KB pages",
        ],
        "tuning_hints": [
            "Enable THP: `echo always > /sys/kernel/mm/transparent_hugepage/enabled`",
            "Use explicit hugepages via hugetlbfs for Redis data",
            "Configure jemalloc with `thp:always` option",
            "For Redis, reserve hugepages at boot: `vm.nr_hugepages=<count>`",
        ],
        "level": 6,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss.Load_STLB_Miss_2M": {
        "description": (
            "Load STLB misses on 2 MB hugepages.  Even with hugepages, the STLB "
            "can miss if data spans many 2 MB regions (>1536 entries typical STLB)."
        ),
        "typical_causes": [
            "Extremely large data set exceeding STLB coverage with 2 MB pages",
            "Redis instance with >3 GB working set actively accessed randomly",
            "Many separate 2 MB mappings fragmenting STLB",
        ],
        "tuning_hints": [
            "Consider 1 GB hugepages for very large data sets (>100 GB)",
            "Reduce active page count by improving data locality",
            "Use `perf stat -e dtlb_load_misses.walk_completed_2m_4m` to quantify",
            "For Redis, 2 MB pages should suffice for most workloads; investigate access patterns if elevated",
        ],
        "level": 6,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss",
    },

    "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss.Load_STLB_Miss_1G": {
        "description": (
            "Load STLB misses on 1 GB hugepages.  Very rare since 1 GB pages "
            "have only 4 STLB entries but cover massive address ranges."
        ),
        "typical_causes": [
            "Extremely large working set (>4 GB actively accessed) on 1 GB pages",
            "Random access across hundreds of GB of data",
        ],
        "tuning_hints": [
            "1 GB page STLB misses indicate a truly massive random-access working set",
            "Ensure enough 1 GB pages are reserved at boot time",
            "Consider data sharding to reduce per-instance working set size",
            "For Redis, this is unusual; check if data access pattern can be improved",
        ],
        "level": 6,
        "parent": "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss",
    },

    # --- Store_STLB_Miss children ---

    "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss.Store_STLB_Miss_4K": {
        "description": (
            "Store STLB misses on 4 KB pages, requiring a full page walk for "
            "the store address translation."
        ),
        "typical_causes": [
            "Write-heavy workload on 4 KB pages exceeding STLB capacity",
            "Scattered stores across many 4 KB pages",
            "First-touch writes to demand-paged memory",
        ],
        "tuning_hints": [
            "Enable THP or use explicit 2 MB hugepages",
            "Pre-fault pages before hot write path",
            "For Redis, ensure data memory is backed by hugepages",
        ],
        "level": 6,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss.Store_STLB_Miss_2M": {
        "description": (
            "Store STLB misses on 2 MB hugepages.  Indicates massive write "
            "footprint exceeding STLB capacity even with 2 MB pages."
        ),
        "typical_causes": [
            "Very large write-heavy working set",
            "RDB save or AOF rewrite writing across many 2 MB regions",
        ],
        "tuning_hints": [
            "Consider 1 GB hugepages for the largest data segments",
            "Reduce write scatter by batching writes to the same memory region",
            "For Redis background save, CoW page duplication can inflate write footprint",
        ],
        "level": 6,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss",
    },

    "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss.Store_STLB_Miss_1G": {
        "description": (
            "Store STLB misses on 1 GB hugepages.  Extremely rare; indicates "
            "write access spanning a vast number of 1 GB regions."
        ),
        "typical_causes": [
            "Multi-hundred-GB working set with scattered writes",
            "Nearly impossible to hit in practice for Redis workloads",
        ],
        "tuning_hints": [
            "If seen, investigate whether the working set can be reduced or writes batched",
            "Ensure 1 GB pages are properly allocated at boot: `hugepagesz=1G hugepages=<N>`",
            "For Redis, this should never be a practical concern",
        ],
        "level": 6,
        "parent": "Backend_Bound.Memory_Bound.Store_Bound.DTLB_Store.Store_STLB_Miss",
    },

    # --- ALU_Op_Utilization children ---

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization.Port_0": {
        "description": (
            "Utilization of execution port 0 (ALU, FP multiply/FMA, vector shift, "
            "division unit on SKL/ICL; varies by microarchitecture)."
        ),
        "typical_causes": [
            "Heavy FP multiply/FMA usage",
            "Integer multiply operations",
            "Vector shift operations",
            "Division operations (shared with divider)",
        ],
        "tuning_hints": [
            "If Port_0 is overloaded, check if some operations can move to other ports",
            "Use compiler hints or intrinsics to balance port pressure",
            "Division is port-0-only; minimize divides in hot loops",
            "For Redis, port 0 pressure from hash computation is typical",
        ],
        "level": 6,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization.Port_1": {
        "description": (
            "Utilization of execution port 1 (ALU, FP add, integer multiply, "
            "vector operations; varies by microarchitecture)."
        ),
        "typical_causes": [
            "Heavy FP add/subtract operations",
            "Integer multiply operations",
            "General ALU operations competing for port 1",
        ],
        "tuning_hints": [
            "Balance FP add and multiply using FMA when possible (reduces port pressure)",
            "Distribute independent operations to reduce port 1 contention",
            "Use `-march=native` to let compiler schedule optimally across ports",
        ],
        "level": 6,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization",
    },

    "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization.Port_6": {
        "description": (
            "Utilization of execution port 6 (ALU, branch execution unit, "
            "integer operations; varies by microarchitecture)."
        ),
        "typical_causes": [
            "Heavy branch execution (many taken branches)",
            "Integer ALU operations scheduled to port 6",
            "Shift and rotate operations",
        ],
        "tuning_hints": [
            "If branch-heavy, reduce branch count with branchless code or cmov",
            "Port 6 is shared between branches and ALU; high branch rate can starve ALU",
            "Use PGO to reduce branch density in hot paths",
            "For Redis, minimize conditional branches in tight command processing loops",
        ],
        "level": 6,
        "parent": "Backend_Bound.Core_Bound.Ports_Utilization.Ports_Utilized_3m.ALU_Op_Utilization",
    },
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_metric_info(metric_name: str) -> Optional[Dict[str, Any]]:
    """Look up a TMA metric by exact name or by partial (leaf) name.

    Exact match is tried first.  If not found, the function searches for
    entries whose dot-separated path ends with the given name.  If multiple
    matches are found, all are returned in a dict keyed by full path.

    Args:
        metric_name: Exact dotted path (e.g., "Backend_Bound.Memory_Bound.L3_Bound")
                     or leaf name (e.g., "L3_Bound").

    Returns:
        A single metric dict if exactly one match is found, a dict of
        {full_path: metric_dict} if multiple matches exist, or None if
        no match is found.
    """
    # Exact match
    if metric_name in METRICS_KB:
        return METRICS_KB[metric_name]

    # Partial / leaf match: find all entries whose path ends with the query
    matches: Dict[str, Dict[str, Any]] = {}
    suffix = f".{metric_name}"
    for key, value in METRICS_KB.items():
        if key == metric_name or key.endswith(suffix):
            matches[key] = value

    if len(matches) == 1:
        return next(iter(matches.values()))
    elif len(matches) > 1:
        return matches
    return None


def list_all_metrics() -> List[str]:
    """Return a sorted list of all TMA metric names (dot-separated paths)."""
    return sorted(METRICS_KB.keys())


def get_children(metric_name: str) -> List[str]:
    """Return the direct children of a given TMA node.

    Accepts either the full dotted path or a leaf name.  If a leaf name
    matches multiple nodes, children from all matches are combined.

    Args:
        metric_name: The node name to find children for.

    Returns:
        A sorted list of full-path names of direct children.
    """
    # Resolve to full path(s)
    full_paths: List[str] = []
    if metric_name in METRICS_KB:
        full_paths.append(metric_name)
    else:
        suffix = f".{metric_name}"
        for key in METRICS_KB:
            if key == metric_name or key.endswith(suffix):
                full_paths.append(key)

    if not full_paths:
        return []

    children: List[str] = []
    for path in full_paths:
        prefix = f"{path}."
        prefix_depth = path.count(".") + 1
        for key in METRICS_KB:
            if key.startswith(prefix) and key.count(".") == prefix_depth:
                children.append(key)

    return sorted(set(children))


def get_parent(metric_name: str) -> Optional[str]:
    """Return the parent node name for a given metric.

    Accepts full dotted path or leaf name.  For leaf names with multiple
    matches, returns the parent of the first match.

    Args:
        metric_name: The metric to find the parent for.

    Returns:
        The parent's full dotted path, or None for Level-1 nodes.
    """
    # Exact match
    if metric_name in METRICS_KB:
        return METRICS_KB[metric_name]["parent"]

    # Partial match
    suffix = f".{metric_name}"
    for key, value in METRICS_KB.items():
        if key == metric_name or key.endswith(suffix):
            return value["parent"]

    return None
