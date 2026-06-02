#!/usr/bin/env swift
// amfi_fetch.swift — download AMFI portfolio holdings using macOS SecureTransport
//
// Usage: swift amfi_fetch.swift <date1> [date2 ...] <output_dir>
//   Dates are AMFI-format strings: "30-Apr-2026"
//   Downloads each URL, writes to <output_dir>/<date>.txt
//   Prints "OK <date>" or "FAIL <date>" to stdout (one per line)
//
import Foundation

guard CommandLine.arguments.count >= 3 else {
    fputs("Usage: amfi_fetch.swift <date1> [date2 ...] <output_dir>\n", stderr)
    exit(1)
}

let args      = Array(CommandLine.arguments.dropFirst())
let outputDir = args.last!
let dates     = Array(args.dropLast())

let fm = FileManager.default
if !fm.fileExists(atPath: outputDir) {
    try? fm.createDirectory(atPath: outputDir, withIntermediateDirectories: true)
}

let session = URLSession(configuration: .default)
let group   = DispatchGroup()

for dateStr in dates {
    let encodedDate = dateStr.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? dateStr
    let urlString = "https://www.amfiindia.com/modules/PortfolioHoldings_new"
                  + "?mfID=0&mfSchemeID=0&asondate=\(encodedDate)&as=1"
    guard let url = URL(string: urlString) else {
        print("FAIL \(dateStr) (bad URL)")
        continue
    }

    var req = URLRequest(url: url, timeoutInterval: 60)
    req.setValue(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        forHTTPHeaderField: "User-Agent"
    )
    req.setValue("text/html,application/xhtml+xml", forHTTPHeaderField: "Accept")

    group.enter()
    let captured = dateStr
    session.dataTask(with: req) { data, response, error in
        defer { group.leave() }

        if let error = error {
            print("FAIL \(captured) (\(error.localizedDescription))")
            return
        }
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let data = data, data.count > 5_000,
              let text = String(data: data, encoding: .utf8) ?? String(data: data, encoding: .isoLatin1)
        else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            print("FAIL \(captured) (HTTP \(code), \(data?.count ?? 0) bytes)")
            return
        }

        let outPath = (outputDir as NSString).appendingPathComponent("\(captured).txt")
        do {
            try text.write(toFile: outPath, atomically: true, encoding: .utf8)
            print("OK \(captured) \(text.count)")
        } catch {
            print("FAIL \(captured) (write error: \(error))")
        }
    }.resume()
}

group.wait()
