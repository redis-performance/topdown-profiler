import Foundation
import Metal

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("Error: \(message)\n".utf8))
    exit(1)
}
let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count == 3, let bits = Int(arguments[0]), [14, 24].contains(bits),
      let threads = Int(arguments[1]), [64, 4096, 262144].contains(threads) else {
    fail("Usage: dependent-reads TABLE_BITS(14|24) THREADS(64|4096|262144) EMPTY_OUTPUT_DIRECTORY")
}
let root = arguments[2]
var directory: ObjCBool = false
guard FileManager.default.fileExists(atPath: root, isDirectory: &directory), directory.boolValue,
      !FileManager.default.fileExists(atPath: root + "/capture.gputrace"),
      !FileManager.default.fileExists(atPath: root + "/probe.json") else {
    fail("Supply an existing output directory with new capture/result paths")
}
let tableCount = 1 << bits
let mask = UInt32(tableCount - 1)
let reads = 16_777_216
var steps = UInt32(reads / threads)
var nativeMask = mask

guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
    fail("No Metal device or command queue")
}
let code = """
#include <metal_stdlib>
using namespace metal;
kernel void dependent_reads(device const uint *next [[buffer(0)]], device uint *output [[buffer(1)]],
                           constant uint &steps [[buffer(2)]], constant uint &mask [[buffer(3)]],
                           uint id [[thread_position_in_grid]]) {
    uint index = id & mask;
    for (uint j = 0; j < steps; ++j) index = next[index];
    output[id] = index;
}
"""
let library = try device.makeLibrary(source: code, options: nil)
guard let function = library.makeFunction(name: "dependent_reads") else { fail("Kernel missing") }
let pipeline = try device.makeComputePipelineState(function: function)
guard let table = device.makeBuffer(length: tableCount * 4, options: .storageModeShared),
      let output = device.makeBuffer(length: threads * 4, options: .storageModeShared) else { fail("Buffer allocation failed") }
let values = table.contents().bindMemory(to: UInt32.self, capacity: tableCount)
for i in 0..<tableCount { values[i] = (UInt32(i) &* 1_664_525 &+ 1_013_904_223) & mask }
let manager = MTLCaptureManager.shared()
guard manager.supportsDestination(.gpuTraceDocument) else { fail("Set MTL_CAPTURE_ENABLED=1") }
let capture = MTLCaptureDescriptor()
capture.captureObject = queue; capture.destination = .gpuTraceDocument
capture.outputURL = URL(fileURLWithPath: root + "/capture.gputrace")
try manager.startCapture(with: capture)
defer { if manager.isCapturing { manager.stopCapture() } }
guard let command = queue.makeCommandBuffer(), let encoder = command.makeComputeCommandEncoder() else { fail("Encoding failed") }
let label = "dependent reads: \(bits) bits, \(threads) threads, \(steps) steps"
command.label = label; encoder.label = label
encoder.setComputePipelineState(pipeline)
encoder.setBuffer(table, offset: 0, index: 0); encoder.setBuffer(output, offset: 0, index: 1)
encoder.setBytes(&steps, length: MemoryLayout<UInt32>.stride, index: 2)
encoder.setBytes(&nativeMask, length: MemoryLayout<UInt32>.stride, index: 3)
encoder.dispatchThreads(MTLSize(width: threads, height: 1, depth: 1),
                        threadsPerThreadgroup: MTLSize(width: min(256, pipeline.maxTotalThreadsPerThreadgroup), height: 1, depth: 1))
encoder.endEncoding(); command.commit(); command.waitUntilCompleted(); manager.stopCapture()
guard command.status == .completed, command.gpuEndTime > command.gpuStartTime else { fail("GPU command failed") }
let actual = output.contents().bindMemory(to: UInt32.self, capacity: threads)
var checksum: UInt64 = 0, expected: UInt64 = 0
var verified = true
for id in 0..<threads {
    var reference = UInt32(id) & mask
    for _ in 0..<Int(steps) { reference = (reference &* 1_664_525 &+ 1_013_904_223) & mask }
    checksum += UInt64(actual[id]); expected += UInt64(reference)
    verified = verified && actual[id] == reference
}
guard verified && checksum == expected else { fail("Dependent-read results differ from CPU recurrence") }
let result: [String: Any] = ["device": device.name, "table_bits": bits, "table_items": tableCount,
                           "table_bytes": tableCount * 4, "threads": threads, "steps": steps,
                           "dependent_reads": reads, "gpu_seconds": command.gpuEndTime - command.gpuStartTime,
                           "checksum": checksum, "expected_checksum": expected,
                           "checksum_verified": verified, "elements_verified": verified]
try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(
    to: URL(fileURLWithPath: root + "/probe.json"), options: .withoutOverwriting)
print(String(data: try JSONSerialization.data(withJSONObject: result, options: .sortedKeys), encoding: .utf8)!)
