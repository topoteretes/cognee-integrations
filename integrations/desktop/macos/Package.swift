// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "CogneeDesktop",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "CogneeDesktop",
            path: "Sources/CogneeDesktop"
        )
    ]
)
