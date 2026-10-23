import Foundation
import Metal

@_silgen_name("stax_metal_begin")
func staxMetalBegin() -> UnsafeMutableRawPointer?
@_silgen_name("stax_metal_finish")
func staxMetalFinish(_ token: UnsafeMutableRawPointer?, _ name: UnsafePointer<CChar>,
                     _ startSeconds: Double, _ endSeconds: Double) -> Bool
@_silgen_name("stax_metal_cancel")
func staxMetalCancel(_ token: UnsafeMutableRawPointer?)

let passes = CommandLine.arguments.count > 1 ? Int(CommandLine.arguments[1]) ?? 0 : 200
guard (1...500).contains(passes), let device = MTLCreateSystemDefaultDevice(),
      let queue = device.makeCommandQueue() else { fatalError("Metal device or pass count unavailable") }
let source = """
#include <metal_stdlib>
using namespace metal;
kernel void add_one(device uint *data [[buffer(0)]], uint id [[thread_position_in_grid]]) {
    data[id] += 1;
}
"""
let library = try device.makeLibrary(source: source, options: nil)
let pipeline = try device.makeComputePipelineState(function: library.makeFunction(name: "add_one")!)
let count = 16_384
let buffer = device.makeBuffer(length: count * MemoryLayout<UInt32>.size, options: .storageModeShared)!
let data = buffer.contents().bindMemory(to: UInt32.self, capacity: count)
for index in 0..<count { data[index] = 0 }
var gated = 0
var reported = 0
for _ in 0..<passes {
    let token = staxMetalBegin()
    if token != nil { gated += 1 }
    guard let command = queue.makeCommandBuffer(), let encoder = command.makeComputeCommandEncoder() else {
        staxMetalCancel(token)
        fatalError("Command buffer unavailable")
    }
    command.label = "M1 Metal add_one"
    encoder.setComputePipelineState(pipeline)
    encoder.setBuffer(buffer, offset: 0, index: 0)
    encoder.dispatchThreads(MTLSize(width: count, height: 1, depth: 1),
                            threadsPerThreadgroup: MTLSize(width: min(256, pipeline.maxTotalThreadsPerThreadgroup),
                                                           height: 1, depth: 1))
    encoder.endEncoding()
    command.commit()
    command.waitUntilCompleted()
    guard command.status == .completed, command.gpuEndTime > command.gpuStartTime else {
        staxMetalCancel(token)
        fatalError("GPU command failed")
    }
    let accepted = command.label!.withCString {
        staxMetalFinish(token, $0, command.gpuStartTime, command.gpuEndTime)
    }
    if accepted { reported += 1 }
    Thread.sleep(forTimeInterval: 0.02)
}
let checksum = (0..<count).reduce(UInt64(0)) { $0 + UInt64(data[$1]) }
let expected = UInt64(passes * count)
print("device=\(device.name) passes=\(passes) checksum=\(checksum) expected=\(expected) gated=\(gated) reported=\(reported)")
if checksum != expected { exit(1) }
