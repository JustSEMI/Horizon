import psutil

class SystemMonitor:
    def __init__(self):
        self.last_net_bytes_recv = 0
        self.last_net_bytes_sent = 0
        
        # Initialize net counters on start
        try:
            net = psutil.net_io_counters()
            self.last_net_bytes_recv = net.bytes_recv
            self.last_net_bytes_sent = net.bytes_sent
        except Exception:
            pass

    def get_stats(self):
        stats = {
            "cpu_percent": 0.0,
            "ram_used_gb": 0.0,
            "ram_total_gb": 0.0,
            "ram_percent": 0.0,
            "dl_bytes": 0,
            "ul_bytes": 0
        }
        
        try:
            # CPU
            stats["cpu_percent"] = psutil.cpu_percent()
            
            # RAM
            mem = psutil.virtual_memory()
            stats["ram_used_gb"] = mem.used / (1024**3)
            stats["ram_total_gb"] = mem.total / (1024**3)
            stats["ram_percent"] = mem.percent
            
            # Network
            net = psutil.net_io_counters()
            stats["dl_bytes"] = net.bytes_recv - self.last_net_bytes_recv
            stats["ul_bytes"] = net.bytes_sent - self.last_net_bytes_sent
            
            self.last_net_bytes_recv = net.bytes_recv
            self.last_net_bytes_sent = net.bytes_sent
        except Exception:
            pass
            
        return stats

    @staticmethod
    def format_speed(bps):
        if bps < 1024:
            return f"{bps} B/s"
        elif bps < 1024**2:
            return f"{bps/1024:.1f} KB/s"
        else:
            return f"{bps/(1024**2):.1f} MB/s"
