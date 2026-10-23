import Foundation
import Metal

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("Error: \(message)\n".utf8))
    exit(1)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count <= 2,
      let passes = arguments.isEmpty ? 4 : Int(arguments[0]),
      (1...500).contains(passes) else {
    fail("Usage: probe [passes 1–500] [alu|stream]")
}
let workload = arguments.count == 2 ? arguments[1] : "alu"
guard workload == "alu" || workload == "stream" else {
    fail("Choose alu or stream workload")
}

guard let device = MTLCreateSystemDefaultDevice(),
      let queue = device.makeCommandQueue() else {
    fail("No Metal GPU or command queue")
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
kernel void stream(device const float *input [[buffer(0)]],
                   device float *output [[buffer(1)]],
                   uint id [[thread_position_in_grid]]) {
    output[id] = input[id] + 1.0f;
}
"""
let library = try device.makeLibrary(source: source, options: nil)
guard let function = library.makeFunction(name: workload == "alu" ? "burn" : "stream") else {
    fail("Metal kernel missing")
}
let pipeline = try device.makeComputePipelineState(function: function)
let count = workload == "alu" ? 16_384 : 16_777_216
guard let buffer = device.makeBuffer(length: count * MemoryLayout<Float>.stride,
                                     options: .storageModeShared) else {
    fail("Could not allocate GPU buffer")
}
let output = workload == "stream" ? device.makeBuffer(length: count * MemoryLayout<Float>.stride,
                                                       options: .storageModeShared) : nil
if workload == "stream" && output == nil {
    fail("Could not allocate GPU output buffer")
}
let values = buffer.contents().bindMemory(to: Float.self, capacity: count)
for i in 0..<count { values[i] = workload == "alu" ? Float(i % 97) / 97.0 : Float(i % 97) }

var totalGPUTime = 0.0
for pass in 0..<passes {
    guard let command = queue.makeCommandBuffer(),
          let encoder = command.makeComputeCommandEncoder() else {
        fail("Could not encode GPU work")
    }
    command.label = "topdown GPU probe pass \(pass)"
    encoder.label = "\(workload == "alu" ? "burn" : "stream") compute pass \(pass)"
    encoder.setComputePipelineState(pipeline)
    encoder.setBuffer(buffer, offset: 0, index: 0)
    if let output {
        encoder.setBuffer(output, offset: 0, index: 1)
    }
    encoder.dispatchThreads(MTLSize(width: count, height: 1, depth: 1),
                            threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1))
    encoder.endEncoding()
    command.commit()
    command.waitUntilCompleted()
    guard command.status == .completed, command.gpuEndTime > command.gpuStartTime else {
        fail("GPU command failed: \(String(describing: command.error))")
    }
    totalGPUTime += command.gpuEndTime - command.gpuStartTime
}

let result = (output ?? buffer).contents().bindMemory(to: Float.self, capacity: count)
let checksum = (0..<count).reduce(0.0) { $0 + Double(result[$1]) }
let expected: Double
if workload == "stream" {
    let cycles = count / 97
    let remainder = count % 97
    expected = Double(cycles * 4656 + remainder * (remainder - 1) / 2 + count)
} else {
    var reference = (0..<97).map { Float($0) / 97.0 }
    for i in 0..<97 {
        for _ in 0..<(passes * 1024) {
            reference[i] = Float(0.000003).addingProduct(reference[i], Float(1.000001))
        }
    }
    expected = (0..<97).reduce(0.0) { sum, i in
        sum + Double(reference[i]) * Double(count / 97 + (i < count % 97 ? 1 : 0))
    }
}
print("workload=\(workload) passes=\(passes) items=\(count) gpu_seconds=\(totalGPUTime) checksum=\(checksum)")
let verified = checksum.isFinite && abs(checksum - expected) <= max(0.000001, abs(expected) * 0.000001)
print("expected_checksum=\(expected) checksum_verified=\(verified)")
if !verified { fail("GPU checksum differs from the CPU reference") }
