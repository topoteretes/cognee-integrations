import Carbon.HIToolbox
import Foundation

/// System-wide hotkey via Carbon's RegisterEventHotKey. Unlike CGEventTap /
/// NSEvent global monitors this needs no Accessibility permission, so the app
/// works on first launch -- important for "clone and try it" testing.
final class GlobalHotKey {
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?
    private let handler: () -> Void
    private let id: UInt32

    /// Default binding: Option+Space (Command+Space stays with the system search).
    /// Each registered hotkey needs its own ``id``.
    init?(
        keyCode: UInt32 = UInt32(kVK_Space),
        modifiers: UInt32 = UInt32(optionKey),
        id: UInt32 = 1,
        handler: @escaping () -> Void
    ) {
        self.handler = handler
        self.id = id

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let selfPointer = Unmanaged.passUnretained(self).toOpaque()
        var installedHandler: EventHandlerRef?
        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData -> OSStatus in
                guard let userData, let event else { return noErr }
                // Every installed handler sees every hotkey press; without this
                // check, whichever hotkey fires triggers ALL registered actions
                // (and the last-installed one swallows the event). Dispatch on
                // the pressed hotkey's ID and pass foreign events along.
                var pressedID = EventHotKeyID()
                GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &pressedID
                )
                let hotKey = Unmanaged<GlobalHotKey>.fromOpaque(userData).takeUnretainedValue()
                guard pressedID.id == hotKey.id else {
                    return OSStatus(eventNotHandledErr)
                }
                DispatchQueue.main.async { hotKey.handler() }
                return noErr
            },
            1,
            &eventType,
            selfPointer,
            &installedHandler
        )
        guard installStatus == noErr else { return nil }
        eventHandler = installedHandler

        let hotKeyID = EventHotKeyID(signature: OSType(0x4347_5350), id: id)  // 'CGSP'
        var registeredRef: EventHotKeyRef?
        let registerStatus = RegisterEventHotKey(
            keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &registeredRef
        )
        guard registerStatus == noErr else {
            if let installedHandler { RemoveEventHandler(installedHandler) }
            return nil
        }
        hotKeyRef = registeredRef
    }

    deinit {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        if let eventHandler { RemoveEventHandler(eventHandler) }
    }
}
