// Foreground (subject) mask via Apple Vision. Usage: swift subject_mask.swift in.jpg out.png
import AppKit
import CoreImage
import Vision

let args = CommandLine.arguments
guard args.count == 3 else { fatalError("usage: subject_mask in out") }
let url = URL(fileURLWithPath: args[1])
guard let ci = CIImage(contentsOf: url) else { fatalError("cannot read") }
let req = VNGenerateForegroundInstanceMaskRequest()
let handler = VNImageRequestHandler(ciImage: ci)
try handler.perform([req])
guard let obs = req.results?.first else { fatalError("no subject") }
print("instances:", obs.allInstances.count)
let buf = try obs.generateScaledMaskForImage(forInstances: obs.allInstances, from: handler)
let mask = CIImage(cvPixelBuffer: buf)
let ctx = CIContext()
let cs = CGColorSpace(name: CGColorSpace.linearGray)!
try ctx.writePNGRepresentation(of: mask, to: URL(fileURLWithPath: args[2]), format: .L8, colorSpace: cs)
print("ok", mask.extent)
