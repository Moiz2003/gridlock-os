import socket

# We'll try the universal broadcast and the subnet-specific one
targets = ['255.255.255.255', '192.168.18.255']
msg = b'{"t":"scan"}'

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.settimeout(2)

print("--- SHOUTING INTO THE VOID ---")
for target in targets:
    try:
        print(f"Scanning {target}...")
        s.sendto(msg, (target, 7000))
        while True:
            data, addr = s.recvfrom(1024)
            print(f"\nFOUND SOMETHING!")
            print(f"IP: {addr[0]}")
            print(f"DETAILS: {data.decode()}")
    except socket.timeout:
        continue

print("\n--- SCAN COMPLETE ---")
print("If nothing was found, your AC is likely in 'Hotspot' mode.")
print("Check your Wi-Fi list for 'Gree-XXXX' or 'AC-XXXX'.")