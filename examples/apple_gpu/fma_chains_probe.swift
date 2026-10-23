import Foundation
import Metal

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("Error: \(message)\n".utf8))
    exit(1)
}
let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count == 3, let threads = Int(arguments[0]), [64, 4096].contains(threads),
      let chains = Int(arguments[1]), [1, 4, 16].contains(chains) else {
    fail("Usage: fma-chains THREADS(64|4096) CHAINS_PER_THREAD(1|4|16) EMPTY_OUTPUT_DIRECTORY")
}
let root = arguments[2]
var directory: ObjCBool = false
guard FileManager.default.fileExists(atPath: root, isDirectory: &directory), directory.boolValue,
      !FileManager.default.fileExists(atPath: root + "/capture.gputrace"),
      !FileManager.default.fileExists(atPath: root + "/probe.json") else {
    fail("Supply an existing output directory with new capture/result paths")
}
let operations = 268_435_456
let count = threads * chains
var steps = UInt32(operations / count)
var multiplier = Float(bitPattern: 0x3f800001)
guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
    fail("No Metal device or command queue")
}
let declarations = (0..<chains).map { "float x\($0) = input[id * \(chains) + \($0)]; const float b\($0) = addends[\($0)];" }.joined(separator: "\n")
let updates = (0..<chains).map { "x\($0) = fma(x\($0), multiplier, b\($0));" }.joined(separator: "\n")
let stores = (0..<chains).map { "output[id * \(chains) + \($0)] = x\($0);" }.joined(separator: "\n")
let code = """
#include <metal_stdlib>
using namespace metal;
kernel void fma_chains(device const float *input [[buffer(0)]], device float *output [[buffer(1)]],
                       constant float *addends [[buffer(2)]], constant float &multiplier [[buffer(3)]],
                       constant uint &steps [[buffer(4)]], uint id [[thread_position_in_grid]]) {
    \(declarations)
    for (uint j = 0; j < steps; ++j) { \(updates) }
    \(stores)
}
"""
let options = MTLCompileOptions()
if #available(macOS 15.0, *) {
    options.mathMode = .safe
    options.mathFloatingPointFunctions = .precise
} else {
    options.fastMathEnabled = false
}
let library = try device.makeLibrary(source: code, options: options)
guard let function = library.makeFunction(name: "fma_chains") else { fail("Kernel missing") }
let pipeline = try device.makeComputePipelineState(function: function)
guard let input = device.makeBuffer(length: count * 4, options: .storageModeShared),
      let output = device.makeBuffer(length: count * 4, options: .storageModeShared),
      let coefficients = device.makeBuffer(length: chains * 4, options: .storageModeShared) else { fail("Buffer allocation failed") }
let values = input.contents().bindMemory(to: Float.self, capacity: count)
let addends = coefficients.contents().bindMemory(to: Float.self, capacity: chains)
for i in 0..<count { values[i] = 0.5 + Float(i % 251) / 257 }
for i in 0..<chains { addends[i] = Float(i + 1) * Float(bitPattern: 0x33800000) }
let manager = MTLCaptureManager.shared()
guard manager.supportsDestination(.gpuTraceDocument) else { fail("Set MTL_CAPTURE_ENABLED=1") }
let capture = MTLCaptureDescriptor()
capture.captureObject = queue; capture.destination = .gpuTraceDocument
capture.outputURL = URL(fileURLWithPath: root + "/capture.gputrace")
try manager.startCapture(with: capture)
defer { if manager.isCapturing { manager.stopCapture() } }
guard let command = queue.makeCommandBuffer(), let encoder = command.makeComputeCommandEncoder() else { fail("Encoding failed") }
let label = "FMA: \(threads) threads, \(chains) chains/thread, \(steps) steps"
command.label = label; encoder.label = label
encoder.setComputePipelineState(pipeline)
encoder.setBuffer(input, offset: 0, index: 0); encoder.setBuffer(output, offset: 0, index: 1)
encoder.setBuffer(coefficients, offset: 0, index: 2)
encoder.setBytes(&multiplier, length: MemoryLayout<Float>.stride, index: 3)
encoder.setBytes(&steps, length: MemoryLayout<UInt32>.stride, index: 4)
encoder.dispatchThreads(MTLSize(width: threads, height: 1, depth: 1),
                       threadsPerThreadgroup: MTLSize(width: min(256, pipeline.maxTotalThreadsPerThreadgroup), height: 1, depth: 1))
encoder.endEncoding(); command.commit(); command.waitUntilCompleted(); manager.stopCapture()
guard command.status == .completed, command.gpuEndTime > command.gpuStartTime else { fail("GPU command failed") }
let actual = output.contents().bindMemory(to: Float.self, capacity: count)
var checksum: UInt64 = 0, expected: UInt64 = 0
for i in 0..<count {
    var reference = values[i]
    for _ in 0..<Int(steps) { reference = addends[i % chains].addingProduct(reference, multiplier) }
    guard actual[i].bitPattern == reference.bitPattern else {
        fail("FMA output \(i) differs from fused CPU reference: \(actual[i].bitPattern) vs \(reference.bitPattern)")
    }
    checksum += UInt64(actual[i].bitPattern); expected += UInt64(reference.bitPattern)
}
let result: [String: Any] = ["device": device.name, "threads": threads, "chains_per_thread": chains,
                           "steps": steps, "logical_fmas": operations, "elements": count,
                           "input_output_bytes": count * 8, "fast_math_enabled": false,
                           "multiplier_bits": multiplier.bitPattern,
                           "addend_bits": (0..<chains).map { addends[$0].bitPattern },
                           "gpu_seconds": command.gpuEndTime - command.gpuStartTime,
                           "checksum": checksum, "expected_checksum": expected,
                           "checksum_verified": true, "elements_bitwise_verified": true]
try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(
    to: URL(fileURLWithPath: root + "/probe.json"), options: .withoutOverwriting)
print(String(data: try JSONSerialization.data(withJSONObject: result, options: .sortedKeys), encoding: .utf8)!)
