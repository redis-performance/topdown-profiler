import Foundation
import Metal
let root = CommandLine.arguments[1]
guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else { fatalError("No Metal device") }
let source = """
#include <metal_stdlib>
using namespace metal;
kernel void increment(device uint *values [[buffer(0)]], uint id [[thread_position_in_grid]]) { values[id] += 1; }
vertex float4 triangle(uint id [[vertex_id]]) { const float2 p[] = {float2(-1,-1),float2(3,-1),float2(-1,3)}; return float4(p[id],0,1); }
fragment float4 red() { return float4(1,0,0,1); }
"""
let library = try device.makeLibrary(source: source, options: nil)
let compute = try device.makeComputePipelineState(function: library.makeFunction(name: "increment")!)
let renderDesc = MTLRenderPipelineDescriptor()
renderDesc.vertexFunction = library.makeFunction(name: "triangle")
renderDesc.fragmentFunction = library.makeFunction(name: "red")
renderDesc.colorAttachments[0].pixelFormat = .rgba8Unorm
let render = try device.makeRenderPipelineState(descriptor: renderDesc)
let count = 4096
let input = device.makeBuffer(length: count * 4, options: .storageModeShared)!
let copy = device.makeBuffer(length: count * 4, options: .storageModeShared)!
input.contents().bindMemory(to: UInt32.self, capacity: count).initialize(repeating: 1, count: count)
let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 64, height: 64, mipmapped: false)
td.storageMode = .shared
td.usage = [.renderTarget, .shaderRead]
let texture = device.makeTexture(descriptor: td)!
let manager = MTLCaptureManager.shared()
let capture = MTLCaptureDescriptor()
capture.captureObject = queue
capture.destination = .gpuTraceDocument
capture.outputURL = URL(fileURLWithPath: root + "/capture.gputrace")
try manager.startCapture(with: capture)
let command = queue.makeCommandBuffer()!
command.label = "topdown mixed encoder validation"
let ce = command.makeComputeCommandEncoder()!
ce.label = "mixed increment"
ce.setComputePipelineState(compute)
ce.setBuffer(input, offset: 0, index: 0)
ce.dispatchThreads(MTLSize(width: count,height: 1,depth: 1), threadsPerThreadgroup: MTLSize(width: 64,height: 1,depth: 1))
ce.endEncoding()
let be = command.makeBlitCommandEncoder()!
be.label = "mixed buffer copy"
be.copy(from: input,sourceOffset: 0,to: copy,destinationOffset: 0,size: count * 4)
be.endEncoding()
let pass = MTLRenderPassDescriptor()
pass.colorAttachments[0].texture = texture
pass.colorAttachments[0].loadAction = .clear
pass.colorAttachments[0].storeAction = .store
let re = command.makeRenderCommandEncoder(descriptor: pass)!
re.label = "mixed offscreen red"
re.setRenderPipelineState(render)
re.drawPrimitives(type: .triangle,vertexStart: 0,vertexCount: 3)
re.endEncoding()
command.commit()
command.waitUntilCompleted()
manager.stopCapture()
guard command.status == .completed else { fatalError("GPU command failed") }
let values = copy.contents().bindMemory(to: UInt32.self,capacity: count)
let verified = (0..<count).allSatisfy { values[$0] == 2 }
var pixel = [UInt8](repeating: 0,count: 4)
texture.getBytes(&pixel,bytesPerRow: 4,from: MTLRegionMake2D(32,32,1,1),mipmapLevel: 0)
guard verified, pixel == [255,0,0,255] else { fatalError("GPU checksums failed") }
let result: [String: Any] = ["device":device.name,"buffer_verified":verified,"pixel":pixel,"gpu_seconds":command.gpuEndTime-command.gpuStartTime,"encoder_types":["compute","blit","render"]]
try JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys]).write(to: URL(fileURLWithPath:root+"/probe.json"),options:.withoutOverwriting)
print(String(data:try JSONSerialization.data(withJSONObject:result),encoding:.utf8)!)
