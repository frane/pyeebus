// Interop harness: a ship-go hub that trusts one remote SKI and echoes SPINE payloads.
package main

import (
	"bufio"
	"crypto/x509"
	"encoding/pem"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/enbility/ship-go/api"
	"github.com/enbility/ship-go/cert"
	"github.com/enbility/ship-go/hub"
	"github.com/enbility/ship-go/mdns"
)

type reader struct{ hub *hub.Hub }

func (r *reader) RemoteServiceConnected(id api.ServiceIdentity) {
	fmt.Println("CONNECTED", id.SKI)
}
func (r *reader) RemoteServiceDisconnected(id api.ServiceIdentity) {
	fmt.Println("DISCONNECTED", id.SKI)
}
func (r *reader) SetupRemoteService(id api.ServiceIdentity, w api.ShipConnectionDataWriterInterface) api.ShipConnectionDataReaderInterface {
	fmt.Println("SETUP", id.SKI)
	return &echo{w}
}
func (r *reader) VisibleRemoteMdnsServicesUpdated(entries []api.RemoteMdnsService) {}
func (r *reader) ServiceUpdated(id api.ServiceIdentity) {
	fmt.Println("SHIPID", id.SKI, id.ShipID)
}
func (r *reader) ServicePairingDetailUpdate(id api.ServiceIdentity, d *api.ConnectionStateDetail) {
	fmt.Println("STATE", id.SKI, d.State())
}
func (r *reader) AllowWaitingForTrust(id api.ServiceIdentity) bool { return true }

type echo struct {
	w api.ShipConnectionDataWriterInterface
}

func (e *echo) HandleShipPayloadMessage(msg []byte) {
	fmt.Println("PAYLOAD", string(msg))
	e.w.WriteShipMessageWithPayload(msg)
}

type stubProvider struct{ cb api.MdnsResolveCB }

func (s *stubProvider) Start(_ api.PairingMode, _ bool, cb api.MdnsResolveCB) bool {
	s.cb = cb
	return true
}
func (s *stubProvider) Shutdown() {}
func (s *stubProvider) AnnounceService(_, _ string, _ int, _ []string) (string, error) {
	return "stub", nil
}
func (s *stubProvider) UnannounceService(_ string) error { return nil }

func main() {
	port := flag.Int("port", 4800, "listen port")
	trust := flag.String("trust", "", "remote SKI to trust")
	dial := flag.String("dial", "", "host:port of remote to dial")
	certOut := flag.String("cert-out", "", "write own certificate PEM here")
	flag.Parse()

	c, err := cert.CreateCertificate("t", "harness", "DE", "harness")
	if err != nil {
		panic(err)
	}
	x, _ := x509.ParseCertificate(c.Certificate[0])
	ski, _ := cert.SkiFromCertificate(x)
	if *certOut != "" {
		_ = os.WriteFile(*certOut, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.Certificate[0]}), 0o644)
	}
	fmt.Println("SKI", ski)

	details, _ := api.NewServiceDetails(ski, "", "")
	m := mdns.NewMDNS(ski, "harness", "harness", "EnergyManagementSystem", "1", nil,
		"harness-ship-id", "harness", *port, []string{"lo"}, mdns.MdnsProviderSelectionTestSetup)
	stub := &stubProvider{}
	m.SetMdnsProvider(stub)
	r := &reader{}
	h, err := hub.NewHub(r, m, *port, c, details, nil, nil)
	if err != nil {
		panic(err)
	}
	r.hub = h
	if err := h.Start(); err != nil {
		fmt.Println("START-WARN", err)
	}
	if *trust != "" {
		h.RegisterRemoteService(api.NewServiceIdentity(*trust, "", ""))
	}
	fmt.Println("READY")
	if *dial != "" && stub.cb != nil {
		// dial only once the test has registered our certificate (it sends a line)
		_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
		host, p, _ := net.SplitHostPort(*dial)
		var portNum int
		fmt.Sscanf(p, "%d", &portNum)
		stub.cb(map[string]string{"txtvers": "1", "id": "py-ship-id", "path": "/ship/", "ski": *trust,
			"register": "false", "brand": "pyeebus", "model": "test", "type": "EnergyManagementSystem"},
			"py", host, "_ship._tcp", []net.IP{net.ParseIP(host)}, portNum, false)
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	h.Shutdown()
}
