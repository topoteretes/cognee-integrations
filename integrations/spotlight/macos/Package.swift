// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "CogneeSpotlight",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "CogneeSpotlight",
            path: "Sources/CogneeSpotlight"
        )
    ]
)
