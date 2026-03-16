import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            MenuBrowsingView()
                .tabItem {
                    Label("Menu", systemImage: "fork.knife")
                }

            FavoritesView()
                .tabItem {
                    Label("Favorites", systemImage: "heart.fill")
                }

            ItemCatalogView()
                .tabItem {
                    Label("Discover", systemImage: "magnifyingglass")
                }
        }
    }
}

#Preview {
    ContentView()
}
