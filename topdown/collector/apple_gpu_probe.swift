import Foundation
import Metal

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("Error: \(message)\n".utf8))
    exit(1)
}

var passes = 4
var count = 16_384
var mode = "compute"
var capturePath: String?
var resultPath: String?
var args = Array(CommandLine.arguments.dropFirst())
if let first = args.first, let value = Int(first) {
    passes = value
    args.removeFirst()
}
while !args.isEmpty {
    let option = args.removeFirst()
    guard !args.isEmpty else { fail("Missing value for \(option)") }
    let value = args.removeFirst()
    switch option {
    case "--mode": mode = value
    case "--items": count = Int(value) ?? 0
    case "--capture": capturePath = value
    case "--result": resultPath = value
    default: fail("Unknown option \(option)")
    }
}
guard (1...500).contains(passes), (1...16_777_216).contains(count), passes * count <= 268_435_456,
      ["compute", "alu", "memory", "stream", "idle"].contains(mode), !(mode == "idle" && capturePath != nil),
      mode != "stream" || count % 64 == 0 else {
    fail("Require 1–500 passes, 1–16777216 items, passes*items<=268435456, mode compute/alu/memory/stream/idle; stream needs items divisible by 64; idle cannot be replay-captured")
}

guard let device = MTLCreateSystemDefaultDevice(),
      let queue = device.makeCommandQueue() else {
    fatalError("No Metal GPU or command queue")
}

print("device=\(device.name)")
for set in device.counterSets ?? [] {
    print("counter_set=\(set.name) counters=\(set.counters.map(\.name).joined(separator: ","))")
}
for (name, point) in [
    ("stage", MTLCounterSamplingPoint.atStageBoundary),
    ("draw", .atDrawBoundary),
    ("dispatch", .atDispatchBoundary),
    ("blit", .atBlitBoundary),
] {
    print("sample_at_\(name)=\(device.supportsCounterSampling(point))")
}

// Keep the same FMA count while reducing loop-control instructions in the ALU control.
let unrolled = Array(repeating: "x = fma(x, 1.000001f, 0.000003f);", count: 32).joined(separator: "\n")
let source = """
#include <metal_stdlib>
using namespace metal;
kernel void burn(device float *values [[buffer(0)]], uint id [[thread_position_in_grid]]) {
    float x = values[id];
    for (uint i = 0; i < 1024; ++i) {
        x = fma(x, 1.000001f, 0.000003f);
    }
    values[id] = x;
}
kernel void alu(device float *values [[buffer(0)]], uint id [[thread_position_in_grid]]) {
    float x = values[id];
    for (uint i = 0; i < 32; ++i) { \(unrolled) }
    values[id] = x;
}
kernel void memory(device const float *input [[buffer(0)]], device float *output [[buffer(1)]],
                   uint id [[thread_position_in_grid]]) {
    output[id] = fma(input[id], 1.000001f, 0.000003f);
}
kernel void stream(device const float4 *input [[buffer(0)]], device float4 *output [[buffer(1)]],
                   constant uint &stride [[buffer(2)]], uint id [[thread_position_in_grid]]) {
    for (uint j = 0; j < 16; ++j) {
        uint offset = id + j * stride;
        output[offset] = fma(input[offset], float4(1.000001f), float4(0.000003f));
    }
}
"""
let library = try device.makeLibrary(source: source, options: nil)
guard let function = library.makeFunction(name: mode == "compute" || mode == "idle" ? "burn" : mode) else {
    fatalError("Metal kernel missing")
}
let pipeline = try device.makeComputePipelineState(function: function)
guard var buffer = device.makeBuffer(length: count * MemoryLayout<Float>.stride, options: .storageModeShared),
      var output = device.makeBuffer(length: count * MemoryLayout<Float>.stride, options: .storageModeShared) else {
    fatalError("Could not allocate GPU buffer")
}
let values = buffer.contents().bindMemory(to: Float.self, capacity: count)
for i in 0..<count { values[i] = Float(i % 97) / 97.0 }

let manager = MTLCaptureManager.shared()
if let path = capturePath {
    guard manager.supportsDestination(.gpuTraceDocument), !FileManager.default.fileExists(atPath: path) else {
        fail("Capture requires MTL_CAPTURE_ENABLED=1 and a new .gputrace path")
    }
    let descriptor = MTLCaptureDescriptor()
    descriptor.captureObject = queue
    descriptor.destination = .gpuTraceDocument
    descriptor.outputURL = URL(fileURLWithPath: path)
    try manager.startCapture(with: descriptor)
}
defer { if manager.isCapturing { manager.stopCapture() } }
var totalGPUTime = 0.0
var commands: [[String: Any]] = []
if mode == "idle" { Thread.sleep(forTimeInterval: 0.25) }
for pass in 0..<(mode == "idle" ? 0 : passes) {
    guard let command = queue.makeCommandBuffer(),
          let encoder = command.makeComputeCommandEncoder() else {
        fatalError("Could not encode GPU work")
    }
    command.label = "topdown GPU \(mode) pass \(pass)"
    encoder.label = "\(mode) compute pass \(pass)"
    encoder.setComputePipelineState(pipeline)
    encoder.setBuffer(buffer, offset: 0, index: 0)
    let streaming = mode == "memory" || mode == "stream"
    if streaming { encoder.setBuffer(output, offset: 0, index: 1) }
    var threadCount = UInt32(mode == "stream" ? count / 64 : count)
    if mode == "stream" { encoder.setBytes(&threadCount, length: MemoryLayout<UInt32>.stride, index: 2) }
    encoder.dispatchThreads(MTLSize(width: Int(threadCount), height: 1, depth: 1),
                            threadsPerThreadgroup: MTLSize(width: min(256, pipeline.maxTotalThreadsPerThreadgroup), height: 1, depth: 1))
    encoder.endEncoding()
    command.commit()
    command.waitUntilCompleted()
    guard command.status == .completed, command.gpuEndTime > command.gpuStartTime else {
        fatalError("GPU command failed: \(String(describing: command.error))")
    }
    totalGPUTime += command.gpuEndTime - command.gpuStartTime
    commands.append(["label": command.label!, "gpu_start_seconds": command.gpuStartTime,
                     "gpu_end_seconds": command.gpuEndTime])
    if streaming { swap(&buffer, &output) }
}

if manager.isCapturing { manager.stopCapture() }
let finalValues = buffer.contents().bindMemory(to: Float.self, capacity: count)
let checksum = (0..<count).reduce(0.0) { $0 + Double(finalValues[$1]) }
var reference = (0..<97).map { Float($0) / 97.0 }
let steps = mode == "idle" ? 0 : passes * (mode == "memory" || mode == "stream" ? 1 : 1024)
for i in 0..<97 {
    for _ in 0..<steps { reference[i] = Float(0.000003).addingProduct(reference[i], Float(1.000001)) }
}
let expected = (0..<97).reduce(0.0) { $0 + Double(reference[$1]) * Double(count / 97 + ($1 < count % 97 ? 1 : 0)) }
let verified = checksum.isFinite && abs(checksum - expected) <= max(0.000001, abs(expected) * 0.000001)
if let path = resultPath {
    let result: [String: Any] = ["device": device.name, "mode": mode, "passes": commands.count, "items": count,
                               "gpu_seconds": totalGPUTime, "checksum": checksum, "expected_checksum": expected,
                               "checksum_verified": verified, "commands": commands]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: URL(fileURLWithPath: path), options: .withoutOverwriting)
}
print("passes=\(commands.count) items=\(count) gpu_seconds=\(totalGPUTime) checksum=\(checksum)")
print("mode=\(mode) checksum_verified=\(verified)")
if !verified { fail("GPU checksum differs from the CPU FMA reference") }
