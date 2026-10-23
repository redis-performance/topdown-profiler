import Foundation
import Metal
guard CommandLine.arguments.count == 2,
      FileManager.default.fileExists(atPath: CommandLine.arguments[1]),
      !FileManager.default.fileExists(atPath: CommandLine.arguments[1] + "/ready"),
      !FileManager.default.fileExists(atPath: CommandLine.arguments[1] + "/stop"),
      !FileManager.default.fileExists(atPath: CommandLine.arguments[1] + "/background-result.json") else {
    FileHandle.standardError.write(Data("Usage: background-render NEW_EXISTING_OUTPUT_DIRECTORY\n".utf8))
    exit(1)
}
let root = CommandLine.arguments[1]
guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else { fatalError("No GPU") }
let code = """
#include <metal_stdlib>
using namespace metal;
vertex float4 triangle(uint id [[vertex_id]]) { const float2 p[] = {float2(-1,-1),float2(3,-1),float2(-1,3)}; return float4(p[id],0,1); }
fragment float4 busy(float4 p [[position]]) { float x=p.x*1e-6f+0.25f; for(uint i=0;i<128;++i) x=fma(x,1.000001f,0.000003f); return float4(x,0,0,1); }
"""
let library = try device.makeLibrary(source:code,options:nil)
let descriptor = MTLRenderPipelineDescriptor()
descriptor.vertexFunction=library.makeFunction(name:"triangle")
descriptor.fragmentFunction=library.makeFunction(name:"busy")
descriptor.colorAttachments[0].pixelFormat = .rgba8Unorm
let pipeline = try device.makeRenderPipelineState(descriptor:descriptor)
let td=MTLTextureDescriptor.texture2DDescriptor(pixelFormat:.rgba8Unorm,width:2048,height:2048,mipmapped:false)
td.storageMode = .shared;td.usage = [.renderTarget]
let texture=device.makeTexture(descriptor:td)!
let limit=Date().addingTimeInterval(40)
var commands=0, seconds=0.0
try Data("ready".utf8).write(to:URL(fileURLWithPath:root+"/ready"))
while Date()<limit && !FileManager.default.fileExists(atPath:root+"/stop") {
 let command=queue.makeCommandBuffer()!;command.label="topdown separate background graphics control"
 let pass=MTLRenderPassDescriptor();pass.colorAttachments[0].texture=texture
 pass.colorAttachments[0].loadAction = .clear;pass.colorAttachments[0].storeAction = .store
 let encoder=command.makeRenderCommandEncoder(descriptor:pass)!
 encoder.setRenderPipelineState(pipeline);encoder.drawPrimitives(type:.triangle,vertexStart:0,vertexCount:3);encoder.endEncoding()
 command.commit();command.waitUntilCompleted()
 guard command.status == .completed else { fatalError("Background GPU command failed") }
 commands+=1;seconds+=command.gpuEndTime-command.gpuStartTime
}
let result:[String:Any] = ["commands":commands,"gpu_seconds":seconds,"completed":true,"device":device.name]
try JSONSerialization.data(withJSONObject:result,options:.prettyPrinted).write(to:URL(fileURLWithPath:root+"/background-result.json"))
